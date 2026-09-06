"""Trusted orchestration integration for direct checkpoint capabilities."""

import base64
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import flow_artifact as crud
from preloop.services.flow_artifacts import artifact_reference, artifact_thread_id


def checkpoint_context(db: Session, context: dict[str, Any]) -> dict[str, str]:
    """Build capabilities solely from server execution identity and prior binding."""
    if not settings.flow_artifact_direct_upload:
        return {}
    from preloop.api.endpoints.flow_artifacts import mint_artifact_capability

    trigger = context.get("trigger_event_data") or {}
    resume = trigger.get("_resume") or {}
    if resume and context.get("checkpoint_resume_authorized") is not True:
        # A legacy PR/CI binding does not authorize dropping unpublished work
        # or pairing its old CLI session with a newly cloned workspace. Durable
        # feedback resumes carry a controller-validated thread reservation.
        # Cold recovery needs an explicit controller decision, not a fallback.
        raise ValueError("checkpoint_resume_not_authorized")
    thread_id = artifact_thread_id(trigger, context["execution_id"])
    identifiers = {
        "account_id": UUID(str(context["account_id"])),
        "flow_id": UUID(str(context["flow_id"])),
        "execution_id": UUID(str(context["execution_id"])),
        "thread_id": thread_id,
        "kind": "workspace",
    }
    env = {
        "PRELOOP_CHECKPOINT_URL": settings.preloop_url.rstrip("/")
        + "/api/v1/flows/executions/"
        + str(context["execution_id"])
        + "/artifacts",
        "PRELOOP_CHECKPOINT_PUT_TOKEN": mint_artifact_capability(
            **identifiers, operation="put"
        ),
        "PRELOOP_CHECKPOINT_MAX_BYTES": str(settings.workspace_snapshot_max_bytes),
        "PRELOOP_CHECKPOINT_EXPANDED_MAX_BYTES": str(
            settings.flow_artifact_expanded_max_bytes
        ),
        "PRELOOP_CHECKPOINT_INTERVAL": str(settings.flow_checkpoint_interval_seconds),
    }
    env["PRELOOP_NATIVE_SESSION_PUT_TOKEN"] = mint_artifact_capability(
        **{**identifiers, "kind": "native_session"}, operation="put"
    )
    native_ref = context.get("native_session_reference")
    if native_ref:
        from preloop.models.schemas.flow_artifact import ArtifactReference

        env["PRELOOP_NATIVE_SESSION_GET_TOKEN"] = mint_artifact_capability(
            **{**identifiers, "kind": "native_session"},
            operation="get",
            reference=ArtifactReference.model_validate(native_ref),
        )
    if resume.get("execution_id"):
        prior = crud.latest(
            db,
            account_id=identifiers["account_id"],
            flow_id=identifiers["flow_id"],
            thread_id=thread_id,
            execution_id=UUID(str(resume["execution_id"])),
            kind="workspace",
        )
        if prior is not None:
            env["PRELOOP_CHECKPOINT_GET_TOKEN"] = mint_artifact_capability(
                **identifiers, operation="get", reference=artifact_reference(prior)
            )
        else:
            # A remote branch does not prove local unpublished work is safe.
            # Cold recovery requires a separate controller-authorized decision.
            raise ValueError("workspace_checkpoint_missing")
    return env


def checkpoint_shell(context: dict[str, Any]) -> str:
    """Install a stdlib client and checkpoint loop before the agent begins."""
    if not context.get("checkpoint_env"):
        return ""
    source = Path(__file__).parents[1] / "agents" / "checkpoint_client.py"
    encoded = base64.b64encode(source.read_bytes()).decode()
    return f"""umask 077
printf '%s' '{encoded}' | base64 -d > /tmp/preloop-checkpoint-client.py
if [ -n "${{PRELOOP_CHECKPOINT_GET_TOKEN:-}}" ]; then
    python3 /tmp/preloop-checkpoint-client.py restore || exit 1
fi
_preloop_checkpoint() {{ python3 /tmp/preloop-checkpoint-client.py capture; }}
_preloop_start_checkpoint_loop() {{
    (while sleep "$PRELOOP_CHECKPOINT_INTERVAL"; do _preloop_checkpoint || true; done) &
    _preloop_checkpoint_pid=$!
}}
trap 'kill "${{_preloop_checkpoint_pid:-}}" 2>/dev/null || true; _preloop_checkpoint || true' EXIT
"""
