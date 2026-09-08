"""Trusted orchestration integration for direct checkpoint capabilities."""

import base64
import json
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
    if resume.get("execution_id") and not context.get(
        "published_branch_handoff_authorized"
    ):
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


def evidence_transport_env(context: dict[str, Any]) -> dict[str, str]:
    """Mint an execution-bound evidence PUT capability for hosted and private runners.

    Workspace checkpoints stay off private runners. Evidence packs are retrieved
    through the account API, so private jobs upload them with the same scoped
    artifact capability used by hosted containers.
    """
    if not settings.flow_artifact_direct_upload:
        return {}
    from preloop.api.endpoints.flow_artifacts import mint_artifact_capability

    trigger = context.get("trigger_event_data") or {}
    thread_id = artifact_thread_id(trigger, context["execution_id"])
    execution_id = str(context["execution_id"])
    token = mint_artifact_capability(
        account_id=UUID(str(context["account_id"])),
        flow_id=UUID(str(context["flow_id"])),
        thread_id=thread_id,
        execution_id=UUID(execution_id),
        kind="evidence",
        operation="put",
    )
    from preloop.cra.evidence_pack import evidence_manifest_context

    # Facts the packer cannot see from inside the container: which files
    # were seeded into the workspace and which source the caller declared.
    # Digests and paths only, no contents.
    manifest_context = json.dumps(
        evidence_manifest_context(trigger, execution_id=execution_id),
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "PRELOOP_EVIDENCE_MANIFEST": manifest_context,
        "PRELOOP_EVIDENCE_URL": (
            settings.preloop_url.rstrip("/")
            + "/api/v1/flows/executions/"
            + execution_id
            + "/artifacts"
        ),
        "PRELOOP_EVIDENCE_PUT_TOKEN": token,
        "PRELOOP_EVIDENCE_MAX_BYTES": str(settings.flow_evidence_max_bytes),
        "PRELOOP_EVIDENCE_EXPANDED_MAX_BYTES": str(
            settings.flow_artifact_expanded_max_bytes
        ),
    }


def _artifact_client_install() -> str:
    """Install the stdlib artifact client at a fixed path (idempotent)."""
    source = Path(__file__).parents[1] / "agents" / "checkpoint_client.py"
    encoded = base64.b64encode(source.read_bytes()).decode()
    return f"""umask 077
if [ ! -f /tmp/preloop-checkpoint-client.py ]; then
printf '%s' '{encoded}' | base64 -d > /tmp/preloop-checkpoint-client.py
fi
"""


def checkpoint_shell(context: dict[str, Any]) -> str:
    """Install a stdlib client and checkpoint loop before the agent begins."""
    if not context.get("checkpoint_env"):
        return ""
    return (
        _artifact_client_install()
        + """
if [ -n "${PRELOOP_CHECKPOINT_GET_TOKEN:-}" ]; then
    python3 /tmp/preloop-checkpoint-client.py restore || exit 1
fi
_preloop_checkpoint() { python3 /tmp/preloop-checkpoint-client.py capture; }
_preloop_upload_evidence() {
    if [ -n "${PRELOOP_EVIDENCE_PUT_TOKEN:-}" ]; then
        python3 /tmp/preloop-checkpoint-client.py evidence || true
    fi
}
_preloop_start_checkpoint_loop() {
    (while sleep "$PRELOOP_CHECKPOINT_INTERVAL"; do _preloop_checkpoint || true; done) &
    _preloop_checkpoint_pid=$!
}
trap 'kill "${_preloop_checkpoint_pid:-}" 2>/dev/null || true; _preloop_checkpoint || true; _preloop_upload_evidence' EXIT
"""
    )


def evidence_shell(context: dict[str, Any]) -> str:
    """Install evidence upload for runs that do not start the checkpoint loop."""
    if not (context.get("evidence_env") or {}).get("PRELOOP_EVIDENCE_PUT_TOKEN"):
        return ""
    if context.get("checkpoint_env"):
        return ""
    return (
        _artifact_client_install()
        + """
_preloop_upload_evidence() { python3 /tmp/preloop-checkpoint-client.py evidence || true; }
trap '_preloop_upload_evidence' EXIT
"""
    )
