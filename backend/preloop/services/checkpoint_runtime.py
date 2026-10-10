"""Trusted orchestration integration for direct checkpoint capabilities."""

import base64
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import flow_artifact as crud
from preloop.services.flow_artifacts import artifact_reference, artifact_thread_id

WORKSPACE_SNAPSHOTS_KEY = "workspace_snapshots"
SNAPSHOT_MODES = frozenset({"always", "when_dirty", "never"})
CLONED_HEADS_PATH = "/tmp/preloop-cloned-heads.json"
_HEAD_SHA = re.compile(r"^[0-9a-fA-F]{40}$|^[0-9a-fA-F]{64}$")


def parse_workspace_snapshots(agent_config: Any) -> str:
    """Read ``agent_config.workspace_snapshots``.

    Args:
        agent_config: The flow's ``agent_config`` value, any shape.

    Returns:
        ``always``, ``when_dirty``, or ``never``. Anything missing or
        unusable is ``when_dirty``: a clean checkout can skip, and a dirty
        tree still snapshots.
    """
    config = agent_config
    if (
        isinstance(config, dict)
        and set(config) == {"agent_config"}
        and isinstance(config.get("agent_config"), dict)
    ):
        config = config["agent_config"]
    if not isinstance(config, Mapping):
        return "when_dirty"
    value = config.get(WORKSPACE_SNAPSHOTS_KEY)
    if isinstance(value, str) and value in SNAPSHOT_MODES:
        return value
    return "when_dirty"


def _commit_sha(value: Any) -> str | None:
    if isinstance(value, str) and _HEAD_SHA.fullmatch(value):
        lowered = value.lower()
        if lowered != "0" * len(lowered):
            return lowered
    return None


def trigger_head_sha(trigger: Any) -> str | None:
    """The commit a pull request or push trigger named, when it is a full SHA."""
    payload = trigger.get("payload", trigger) if isinstance(trigger, dict) else None
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = []
    head_commit = payload.get("head_commit")
    if isinstance(head_commit, dict):
        candidates.append(head_commit.get("id"))
    attributes = payload.get("object_attributes")
    if isinstance(attributes, dict):
        last_commit = attributes.get("last_commit")
        if isinstance(last_commit, dict):
            candidates.append(last_commit.get("id"))
        candidates.append(attributes.get("sha"))
    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        if isinstance(head, dict):
            candidates.append(head.get("sha"))
    candidates.append(payload.get("sha"))
    candidates.append(payload.get("after"))
    for candidate in candidates:
        sha = _commit_sha(candidate)
        if sha:
            return sha
    return None


def _relative_clone_path(clone_path: Any, index: int) -> str | None:
    raw = clone_path if isinstance(clone_path, str) and clone_path else None
    if raw is None:
        return "." if index == 0 else None
    if raw == "/workspace":
        return "."
    prefix = "/workspace/"
    if not raw.startswith(prefix):
        return None
    relative = raw[len(prefix) :].strip("/")
    if not relative or ".." in relative.split("/"):
        return None
    return relative


def expected_clone_heads(context: Mapping[str, Any]) -> dict[str, str]:
    """Workspace-relative path to the commit the control plane expects to clone.

    The clone records the real HEAD afterwards. This map is the fallback when
    that file is missing. A single repository inherits the trigger SHA. Extra
    repositories without their own pin are left unset so a guess cannot skip
    a checkout that was not the one we cloned.
    """
    git_config = context.get("git_clone_config") or {}
    if not isinstance(git_config, dict):
        git_config = {}
    repositories = git_config.get("repositories") or []
    trigger_sha = trigger_head_sha(context.get("trigger_event_data"))
    if not isinstance(repositories, list) or not repositories:
        return {".": trigger_sha} if trigger_sha else {}
    usable = [repo for repo in repositories if isinstance(repo, dict)]
    heads: dict[str, str] = {}
    for index, repo in enumerate(usable):
        sha = _commit_sha(repo.get("commit")) or _commit_sha(repo.get("pin_sha"))
        if sha is None and len(usable) == 1:
            sha = trigger_sha
        if sha is None:
            continue
        relative = _relative_clone_path(repo.get("clone_path"), index)
        if relative:
            heads[relative] = sha
    return heads


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
    mode = parse_workspace_snapshots(context.get("agent_config"))
    env = {
        "PRELOOP_CHECKPOINT_URL": settings.preloop_url.rstrip("/")
        + "/api/v1/flows/executions/"
        + str(context["execution_id"])
        + "/artifacts",
        "PRELOOP_CHECKPOINT_MAX_BYTES": str(settings.workspace_snapshot_max_bytes),
        "PRELOOP_CHECKPOINT_EXPANDED_MAX_BYTES": str(
            settings.flow_artifact_expanded_max_bytes
        ),
        "PRELOOP_CHECKPOINT_INTERVAL": str(settings.flow_checkpoint_interval_seconds),
        "PRELOOP_WORKSPACE_SNAPSHOTS": mode,
        "PRELOOP_CHECKPOINT_CLONED_HEADS": CLONED_HEADS_PATH,
    }
    heads = expected_clone_heads(context)
    if heads:
        env["PRELOOP_CHECKPOINT_EXPECTED_HEADS"] = json.dumps(heads, sort_keys=True)
    # ``never`` must not be able to upload customer source. Native session
    # tokens below are a different artifact kind.
    if mode != "never":
        env["PRELOOP_CHECKPOINT_PUT_TOKEN"] = mint_artifact_capability(
            **identifiers, operation="put"
        )
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


_CHECKPOINT_ARMED = """
_preloop_checkpoint() { python3 /tmp/preloop-checkpoint-client.py capture; }
_preloop_start_checkpoint_loop() {
    (while sleep "$PRELOOP_CHECKPOINT_INTERVAL"; do _preloop_checkpoint || true; done) &
    _preloop_checkpoint_pid=$!
}
"""

_CHECKPOINT_NEVER = """
_preloop_checkpoint() { echo PRELOOP_CHECKPOINT skipped workspace_snapshots_never; }
_preloop_start_checkpoint_loop() { :; }
"""


def checkpoint_shell(context: dict[str, Any]) -> str:
    """Install a stdlib client and checkpoint loop before the agent begins."""
    env = context.get("checkpoint_env") or {}
    if not env:
        return ""
    mode = env.get("PRELOOP_WORKSPACE_SNAPSHOTS", "when_dirty")
    definitions = _CHECKPOINT_NEVER if mode == "never" else _CHECKPOINT_ARMED
    return (
        _artifact_client_install()
        + """
if [ -n "${PRELOOP_CHECKPOINT_GET_TOKEN:-}" ]; then
    python3 /tmp/preloop-checkpoint-client.py restore || exit 1
fi
"""
        + definitions
        + """
_preloop_upload_evidence() {
    # The Kubernetes wrapper exports this before the child starts and PUTs
    # the pack itself. Docker never sets it, so this trap stays the upload.
    if [ -n "${PRELOOP_EVIDENCE_WRAPPER_OWNS_UPLOAD:-}" ]; then
        return 0
    fi
    if [ -n "${PRELOOP_EVIDENCE_PUT_TOKEN:-}" ]; then
        python3 /tmp/preloop-checkpoint-client.py evidence || true
    fi
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
_preloop_upload_evidence() {
    if [ -n "${PRELOOP_EVIDENCE_WRAPPER_OWNS_UPLOAD:-}" ]; then
        return 0
    fi
    python3 /tmp/preloop-checkpoint-client.py evidence || true
}
trap '_preloop_upload_evidence' EXIT
"""
    )
