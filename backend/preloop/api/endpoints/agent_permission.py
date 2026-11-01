"""Permission-check endpoint for onboarded agents' native tool calls.

Onboarded agents call ``POST /api/v1/agents/permission-check`` (authenticated
with their managed-agent runtime bearer token) before executing a native tool.
The request is evaluated and, when human approval is required, routed through
the existing approval pipeline to mobile/watch; the endpoint blocks until a
decision (or timeout) and returns a simple allow/deny.
"""

import logging
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from preloop.api.auth.jwt import (
    _authenticate_with_api_key,
    _managed_agent_for_api_key,
    _runtime_session_id_from_api_key,
)
from preloop.api.loop_safety import run_db_off_loop
from preloop.api.auth.key_scopes import enforce_runtime_api_key_scope
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_api_key,
    crud_managed_agent,
    crud_runtime_session,
)
from preloop.models.db.session import get_session_factory
from preloop.services import operator_notes
from preloop.services.agent_permission_service import request_agent_permission
from preloop.services.session_hook_events import (
    is_prompt_event,
    record_prompt,
    record_session_event,
)
from preloop.services.flow_approval_policy import (
    FlowApprovalGrant,
    resolve_flow_grant,
    tool_call_within_policy,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class PermissionIdentity:
    """Authenticated scalars safe to retain while awaiting a human decision."""

    account_id: str
    user_id: Optional[UUID]
    api_key_id: UUID
    managed_agent_id: Optional[UUID]
    runtime_session_id: Optional[UUID]
    managed_agent_name: str
    runtime_principal_type: Optional[str] = None
    runtime_principal_id: Optional[str] = None
    flow_id: Optional[UUID] = None
    #: Set only for an execution-scoped flow credential, i.e. an unattended
    #: run. Approval-required calls park this execution.
    flow_execution_id: Optional[UUID] = None


def _flow_execution_id_from_context(context: dict) -> Optional[UUID]:
    """Return the execution id an execution-scoped flow token names."""
    if not context.get("flow_execution_id") or not context.get("flow_id"):
        return None
    try:
        return UUID(str(context.get("flow_execution_id")))
    except ValueError:
        return None


def _flow_id_from_context(context: dict) -> Optional[UUID]:
    """Return the flow id of an execution-scoped flow token, if valid."""
    if not context.get("flow_execution_id") or not context.get("flow_id"):
        return None
    try:
        return UUID(str(context.get("flow_id")))
    except ValueError:
        return None


def _resolve_permission_identity(token: str) -> PermissionIdentity:
    """Authenticate in one worker-owned session, closed before approval waits."""
    with get_session_factory()() as db:
        api_key = crud_api_key.get_by_key(db, key=token)
        user = _authenticate_with_api_key(db, api_key, allow_managed_agent=True)
        enforce_runtime_api_key_scope(api_key)
        managed_agent = _managed_agent_for_api_key(db, api_key)
        runtime_session = None
        runtime_session_id = _runtime_session_id_from_api_key(api_key)
        if runtime_session_id is not None:
            runtime_session = crud_runtime_session.get_account_session(
                db,
                account_id=api_key.account_id,
                runtime_session_id=runtime_session_id,
            )
        context = api_key.context_data if isinstance(api_key.context_data, dict) else {}
        principal = context.get("runtime_principal")
        principal = principal if isinstance(principal, dict) else {}
        if managed_agent is None or context.get("flow_execution_id"):
            # Flow keys are ephemeral and account-scoped, and must name the
            # exact execution session they were issued for. Ordinary API keys
            # still cannot use this native approval endpoint.
            execution_id = context.get("flow_execution_id")
            if not (
                execution_id
                and runtime_session is not None
                and runtime_session.session_source_type == "flow_execution"
                and runtime_session.session_source_id == str(execution_id)
                and runtime_session.ended_at is None
            ):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Token is not bound to a managed agent or active flow execution",
                    headers={"WWW-Authenticate": "Bearer"},
                )
        return PermissionIdentity(
            account_id=str(api_key.account_id),
            user_id=user.id,
            api_key_id=api_key.id,
            managed_agent_id=managed_agent.id if managed_agent else None,
            runtime_session_id=runtime_session.id
            if runtime_session
            else runtime_session_id,
            flow_id=_flow_id_from_context(context),
            flow_execution_id=_flow_execution_id_from_context(context),
            runtime_principal_type=principal.get("type")
            or getattr(managed_agent, "session_source_type", None),
            runtime_principal_id=principal.get("id")
            or getattr(managed_agent, "session_source_id", None),
            managed_agent_name=(
                getattr(managed_agent, "display_name", None)
                or getattr(managed_agent, "name", None)
                or (runtime_session.runtime_principal_name if runtime_session else None)
                or "Agent"
            ),
        )


def _origin_matches_principal(
    session: models.RuntimeSession,
    principal_type: Optional[str],
    principal_id: Optional[str],
    gateway_source_id: Optional[str] = None,
) -> bool:
    """Reject another principal's row; legacy unbound rows remain compatible."""
    return (
        not session.runtime_principal_type
        or session.runtime_principal_type == principal_type
    ) and (
        not session.runtime_principal_id
        or session.runtime_principal_id in {principal_id, gateway_source_id}
    )


@dataclass(frozen=True)
class OriginSession:
    """The recorded session a hook call belongs to.

    ``hosted_runner`` is True when it is a runner-hosted session (#1482): the
    hook's approvals and notes then belong to that session itself.
    """

    runtime_session_id: str
    hosted_runner: bool = False


def _origin_runtime_session_id(
    identity: PermissionIdentity, source: Optional[str], session_id: str
) -> Optional[OriginSession]:
    """Link a hook session only to a recorded session in the caller's account."""
    from preloop.services.agent_session_lineage import runner_session_for_external

    source_type = {"codex_cli": "codex"}.get(source or "", source)
    with get_session_factory()() as db:
        # A runner-hosted harness (#1482) reports the id the runner minted;
        # its approvals and notes belong on the runner session itself.
        hosted = runner_session_for_external(
            db,
            account_id=identity.account_id,
            external_session_id=session_id,
            actor_user_id=identity.user_id,
        )
        if hosted is not None:
            return OriginSession(str(hosted.id), hosted_runner=True)
        principal_type = identity.runtime_principal_type
        principal_id = identity.runtime_principal_id
        if (not principal_type or not principal_id) and identity.runtime_session_id:
            base = crud_runtime_session.get_account_session(
                db,
                account_id=identity.account_id,
                runtime_session_id=identity.runtime_session_id,
            )
            if base is not None:
                principal_type = base.runtime_principal_type or base.session_source_type
                principal_id = base.runtime_principal_id or base.session_source_id
        # Gateway sessions use the authenticated durable principal plus run id.
        # A hook's declared source never substitutes for that trusted principal.
        if principal_type and principal_id:
            session = crud_runtime_session.get_by_source(
                db,
                account_id=identity.account_id,
                session_source_type=principal_type,
                session_source_id=f"{principal_id}:{session_id}",
            )
            if session is not None and _origin_matches_principal(
                session, principal_type, principal_id, f"{principal_id}:{session_id}"
            ):
                return OriginSession(str(session.id))
        # Usage importers and host observers record a bare native session id.
        if source_type:
            session = crud_runtime_session.get_by_source(
                db,
                account_id=identity.account_id,
                session_source_type=source_type,
                session_source_id=session_id,
            )
            if session is not None and _origin_matches_principal(
                session, principal_type, principal_id
            ):
                return OriginSession(str(session.id))
        return None


def _current_agent_session_id(identity: PermissionIdentity) -> Optional[UUID]:
    """Return the managed agent's current open session, account scoped.

    Never raises: without a session the check still decides the call, it is
    only unattributed, which is how it behaved before.
    """
    try:
        with get_session_factory()() as db:
            agent = crud_managed_agent.get_for_account(
                db,
                account_id=identity.account_id,
                agent_id=str(identity.managed_agent_id),
            )
            if agent is None or agent.runtime_session_id is None:
                return None
            session = crud_runtime_session.get_account_session(
                db,
                account_id=identity.account_id,
                runtime_session_id=agent.runtime_session_id,
            )
            if session is None or session.ended_at is not None:
                return None
            return session.id
    except Exception:
        logger.warning("Agent session lookup failed on permission check", exc_info=True)
        return None


def _records_native_tool_call(payload: "AgentPermissionCheckRequest") -> bool:
    """Whether this check is the one record of its tool call.

    Codex asks twice for an escalated call: a PreToolUse rules check and then
    a PermissionRequest. The first already recorded the call; the approval
    the second raises is its own timeline item.
    """
    source = (payload.source or "").strip()
    return not (
        source == "codex_cli" and payload.evaluation_phase == "permission_request"
    )


def _native_tool_status(
    decision: str, approval_request_id: Optional[str], timed_out: bool
) -> str:
    """Name the outcome of a native permission check for the timeline."""
    if approval_request_id:
        if decision == "allow":
            return "approved"
        return "timed_out" if timed_out else "declined"
    return "allowed" if decision == "allow" else "denied"


def _record_native_tool_call(
    identity: PermissionIdentity,
    *,
    runtime_session_id: Any,
    payload: "AgentPermissionCheckRequest",
    decision: str,
    reason: Optional[str],
    approval_request_id: Optional[str],
    timed_out: bool,
    elapsed_ms: int,
) -> None:
    """Put one native tool call on its session timeline and live stream (#1149).

    MCP calls through Preloop were already recorded and broadcast; a native
    call decided by a hook was neither, so a watcher saw approvals but not
    the calls around them. The row has the same shape as an MCP tool call:
    tool name, outcome, and an arguments summary of key names and sizes only,
    never the values. The live event is the ``runtime_session_updated`` event
    MCP calls already emit, with the decision fields added.

    Never raises: the decision has been made and must reach the agent.
    """
    from preloop.models.crud import crud_runtime_session_activity
    from preloop.services.account_realtime import (
        ACCOUNT_TOPIC_RUNTIME_SESSIONS,
        build_account_event,
        emit_account_event,
    )
    from preloop.services.dynamic_fastmcp import (
        _bounded_summary,
        _hash_arguments,
        _summarize_arguments,
    )

    arguments = dict(payload.tool_input or {})
    source = (payload.source or "").strip() or "native"
    status_label = _native_tool_status(decision, approval_request_id, timed_out)
    metadata: dict[str, Any] = {
        "origin": "native_hook",
        "source": source,
        "decision": decision,
        "evaluation_phase": payload.evaluation_phase,
        "duration_ms": elapsed_ms,
        "arguments_summary": _summarize_arguments(arguments),
        "arguments_hash": _hash_arguments(arguments),
    }
    if approval_request_id:
        metadata["approval_request_id"] = approval_request_id
    work_ref = _native_work_ref(payload)
    if work_ref is not None:
        metadata["work_ref"] = work_ref
    try:
        with get_session_factory()() as db:
            activity = crud_runtime_session_activity.log_tool_call(
                db,
                account_id=identity.account_id,
                runtime_session_id=runtime_session_id,
                api_key_id=identity.api_key_id,
                server_name=source,
                tool_name=(payload.tool_name or "")[:255] or None,
                status=status_label,
                summary=_bounded_summary(reason),
                metadata=metadata,
            )
            timestamp = activity.timestamp.isoformat() if activity.timestamp else None
            activity_id = str(activity.id)
    except Exception:
        logger.warning("Recording the native tool call failed", exc_info=True)
        return
    emit_account_event(
        build_account_event(
            account_id=identity.account_id,
            topic=ACCOUNT_TOPIC_RUNTIME_SESSIONS,
            event_type="runtime_session_updated",
            payload={
                "runtime_session_id": str(runtime_session_id),
                "runtime_principal_type": identity.runtime_principal_type,
                "runtime_principal_id": identity.runtime_principal_id,
                "runtime_principal_name": identity.managed_agent_name,
                "last_activity_at": timestamp,
                "activity_id": activity_id,
                "managed_agent_id": str(identity.managed_agent_id)
                if identity.managed_agent_id
                else None,
                "flow_id": str(identity.flow_id) if identity.flow_id else None,
                "activity_type": "tool_call",
                "tool_name": payload.tool_name,
                "server_name": source,
                "status": status_label,
                "summary": _bounded_summary(reason),
                "metadata": metadata,
            },
            runtime_session_id=str(runtime_session_id),
        )
    )
    if work_ref is not None and status_label in {"allowed", "approved"}:
        from preloop.services.fleet_code_activity import emit_code_tool_event

        # Recorded at PreToolUse: the command was allowed, not yet observed
        # to succeed (no PostToolUse hook reaches this endpoint).
        emit_code_tool_event(
            account_id=identity.account_id,
            tool_name=payload.tool_name,
            metadata=metadata,
            managed_agent_id=identity.managed_agent_id,
            flow_id=identity.flow_id,
            runtime_session_id=runtime_session_id,
            timestamp=timestamp,
        )


#: Native tools whose input is a shell command line.
SHELL_TOOL_NAMES = frozenset(
    {
        "bash",
        "shell",
        "run_shell_command",
        "run_terminal_cmd",
        "execute_command",
        "exec_command",
        "terminal",
        "local_shell",
    }
)


def _native_work_ref(
    payload: "AgentPermissionCheckRequest",
) -> Optional[dict[str, Any]]:
    """Classify a ``gh``/``glab`` command in a native shell tool call.

    Only the parsed verb, object reference and repository are returned; the
    command line itself is never kept. Never raises.
    """
    if (payload.tool_name or "").strip().lower() not in SHELL_TOOL_NAMES:
        return None
    tool_input = payload.tool_input or {}
    command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    from preloop.services.fleet_work_refs import classify_shell_command

    remote = payload.repository.remote if payload.repository is not None else None
    return classify_shell_command(command, remote=remote)


#: Longest working directory stored on a session; matches the column.
MAX_SESSION_CWD_CHARS = 1024


def _record_session_cwd(
    identity: PermissionIdentity, runtime_session_id: Any, cwd: str
) -> None:
    """Remember the working directory the hook reported for its session.

    The session list uses it to label sessions that have no title yet, so two
    runs started in the same second can be told apart (#1148). One guarded
    UPDATE: it writes only when the value changed, is bounded to the caller's
    account, and never raises, because a label must not turn a permission
    check into a denied tool call.
    """
    value = cwd.strip()[:MAX_SESSION_CWD_CHARS]
    if not value:
        return
    try:
        with get_session_factory()() as db:
            db.query(models.RuntimeSession).filter(
                models.RuntimeSession.id == runtime_session_id,
                models.RuntimeSession.account_id == identity.account_id,
                models.RuntimeSession.cwd.is_distinct_from(value),
                # A runner session shows its workspace label, never the host
                # path the runner's harness reports (#1482).
                models.RuntimeSession.session_source_type != "runner_session",
            ).update({models.RuntimeSession.cwd: value}, synchronize_session=False)
            db.commit()
    except Exception:
        logger.warning("Recording the session cwd failed", exc_info=True)


def _claim_operator_note(
    identity: PermissionIdentity,
    origin_session_id: Optional[str] = None,
    *,
    defer_delivery: bool = False,
    hosted_origin: bool = False,
) -> Optional[str]:
    """Claim this session's pending operator notes for the hook channel.

    The hook's own conversation can be recorded as a session of its own (the
    origin session, from the hook's ``session_id``), and that is the session
    an operator finds in ``preloop sessions list`` and attaches to. A resolved
    origin owns the claim; only an unresolved origin uses the credential's
    session binding. A native conversation never claims another session's notes.

    Never raises: a note is a bonus on this route, and a store problem must
    not turn a permission check into a denied tool call.
    """
    if identity.managed_agent_id is None and not (hosted_origin and origin_session_id):
        # A runner-hosted session has no managed agent; its notes are
        # addressed to the session and still reach the hook (#1482).
        return None
    # A known native conversation owns its claims. The bearer binding may be
    # a separate enrollment identity, or another session the token outlived.
    session_ids: list[Optional[str]] = [
        origin_session_id
        or (str(identity.runtime_session_id) if identity.runtime_session_id else None)
    ]
    # Claim only the authoritative scope. A storage failure leaves the
    # permission decision intact and must not trigger a broader-scope fallback.
    notes: list[Any] = []
    for session_id in session_ids:
        try:
            with get_session_factory()() as db:
                claimed = operator_notes.claim_pending_notes(
                    db,
                    account_id=identity.account_id,
                    managed_agent_id=(
                        str(identity.managed_agent_id)
                        if identity.managed_agent_id
                        else None
                    ),
                    runtime_session_id=session_id,
                    channel=operator_notes.CHANNEL_HOOK,
                    defer_delivery=defer_delivery,
                    event=operator_notes.EVENT_PRE_TOOL_USE,
                )
                if claimed:
                    # Rendered while the session is open; the rows expire
                    # on close.
                    notes.append(operator_notes.render_notes_block(claimed))
        except Exception:
            logger.warning(
                "Operator note claim failed on permission check", exc_info=True
            )
    return "\n".join(notes) if notes else None


def _permission_check_base_url() -> str:
    """Resolve the public Preloop base URL used in approval notifications."""
    base_url = (settings.preloop_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PRELOOP_URL is not configured",
        )
    return base_url


class AgentPermissionRepository(BaseModel):
    """Trusted hook observation of the repository a native call ran in.

    The hook resolves its own ``cwd`` against git and sends the result; the
    caller-supplied tool arguments are never used, so an MCP tool cannot
    spoof the repository. Extra keys are forbidden and every string is
    bounded: the object travels into approval ``tool_args`` and onto the
    session timeline, and this is untrusted network input.
    """

    model_config = ConfigDict(extra="forbid")

    remote: str = Field(
        "",
        max_length=512,
        description=(
            "Normalized 'host/owner/repo' with credentials stripped; empty "
            "when the work tree has no origin remote."
        ),
    )
    toplevel: Optional[str] = Field(
        None, max_length=512, description="Absolute work-tree root."
    )
    relative_path: Optional[str] = Field(
        None, max_length=512, description="cwd relative to the work-tree root."
    )
    source: Optional[str] = Field(
        None,
        max_length=512,
        description="How the identity was observed, e.g. 'hook_cwd'.",
    )
    no_remote: bool = Field(
        False,
        description=(
            "True when the work tree has no origin, or the origin is not a "
            "host/owner/repo identity (a local path or file:// remote)."
        ),
    )

    @field_validator("remote", "toplevel", "relative_path", "source")
    @classmethod
    def _at_most_512_bytes(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and len(value.encode("utf-8")) > 512:
            raise ValueError("must be at most 512 bytes")
        return value


class AgentPermissionCheckRequest(BaseModel):
    """A native tool-call permission check from an onboarded agent."""

    tool_name: str = Field(..., description="Native tool name, e.g. 'Bash'")
    tool_input: Dict[str, Any] = Field(
        default_factory=dict, description="Tool arguments (stored as tool_args)"
    )
    source: Optional[str] = Field(
        None,
        description=(
            "Originating agent adapter: 'claude_code', 'codex_cli', 'cursor', "
            "'opencode', 'openclaw', 'hermes', 'pi', or 'deepseek'. Stored as the "
            "'_preloop_source' marker inside tool_args so approver surfaces "
            "can label the requester."
        ),
    )
    hook_event_name: Optional[str] = Field(
        None,
        max_length=64,
        description="Harness event that will carry the response, used for delivery receipts.",
    )
    session_id: Optional[str] = Field(
        None, max_length=255, description="Originating agent session id"
    )
    model: Optional[str] = Field(
        None, max_length=255, description="Originating turn model observed by the hook"
    )
    cwd: Optional[str] = Field(None, description="Working directory")
    flow_execution_id: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "Persistent flow execution the session runs for, from "
            "PRELOOP_FLOW_EXECUTION_ID. Lets that flow's approval policy "
            "apply to this session only."
        ),
    )
    repository: Optional[AgentPermissionRepository] = Field(
        None,
        description=(
            "Trusted repository identity the hook observed from its cwd. "
            "Stored as the '_preloop_repository' marker inside tool_args, next "
            "to '_preloop_source', so approvals and the session timeline can "
            "label which repository the call ran in."
        ),
    )
    agent_reasoning: Optional[str] = Field(
        None, description="Why the agent wants this call (shown to the approver)"
    )
    client_decision: Optional[str] = Field(
        None,
        description=(
            "What the client's own policy decided: 'allow' | 'deny' | 'ask'. "
            "Absent/'ask' escalates to a human approver."
        ),
    )

    approval_request_id: Optional[UUID] = Field(
        None,
        description=(
            "Id the hook proposes for an approval this call may create, so it "
            "can expire that approval through POST /approvals/{id}/expire when "
            "its own deadline passes. Ignored when the id is taken."
        ),
    )

    evaluation_phase: Literal["permission_request", "pre_tool_use"] = Field(
        "permission_request",
        description=(
            "'pre_tool_use' checks central native rules without assuming a host "
            "permission decision: no matching rule continues without automatic "
            "human escalation. 'permission_request' retains normal remote "
            "escalation. A pre-tool allow never grants the host's permission."
        ),
    )


class AgentPermissionCheckResponse(BaseModel):
    """Allow/deny decision for the agent's native tool call."""

    decision: str = Field(..., description="'allow' or 'deny'")
    reason: str = ""
    request_id: Optional[str] = None
    operator_note: Optional[str] = Field(
        None,
        description=(
            "A pending operator note, rendered for the model, to surface as "
            "additional context alongside this decision. Null when there is "
            "none, which is almost always: the PreToolUse call the agent was "
            "making anyway carries the note, so a note costs no extra round "
            "trip and no note costs nothing at all."
        ),
    )
    decided_by: Optional[str] = Field(
        None,
        description=(
            "Who decided an approval this call waited on: a person's username, "
            "'AI review' or 'policy'. Null when no approval was involved or it "
            "expired. Hooks show it in the terminal."
        ),
    )
    timed_out: bool = Field(
        False,
        description=(
            "True when the deny is only the expiry of an unanswered approval "
            "request, not a human decision. It remains a denial; adapters must "
            "not replace required central approval with a local prompt."
        ),
    )
    parked: bool = Field(
        False,
        description=(
            "True when the call needed a human and the unattended flow "
            "execution behind this credential is being paused on approval "
            "request_id. The decision is 'deny': the command must not run. "
            "The run resumes on approval and fails closed on refusal or expiry."
        ),
    )


@router.post(
    "/agents/permission-check",
    response_model=AgentPermissionCheckResponse,
    tags=["Agent Permissions"],
)
async def agent_permission_check(
    payload: AgentPermissionCheckRequest,
    authorization: Optional[str] = Header(None),
) -> AgentPermissionCheckResponse:
    """Decide whether an onboarded agent's native tool call may proceed."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()

    # Preserve the existing credential/binding checks, but keep all ORM access
    # and pool waits off the event loop. No session survives this await.
    identity = await run_db_off_loop(lambda: _resolve_permission_identity(token))
    if identity.runtime_session_id is None and identity.managed_agent_id is not None:
        # A durable hook credential names the agent but no session. Without a
        # session, a note sent to the agent's session is never claimed here,
        # an approval is unattributed and the call is invisible to anyone
        # watching the session. Use the agent's current open session, the
        # same one ``notes send --agent`` resolves to.
        agent_session_id = await run_db_off_loop(
            lambda: _current_agent_session_id(identity)
        )
        if agent_session_id is not None:
            identity = replace(identity, runtime_session_id=agent_session_id)
    started = time.monotonic()

    tool_input = dict(payload.tool_input or {})
    # Caller-supplied tool arguments must never carry the trust markers: only
    # the validated ``source``/``repository`` fields may set them, so a native
    # tool argument cannot spoof the adapter or repository chip.
    tool_input.pop("_preloop_repository", None)
    tool_input.pop("_preloop_source", None)
    tool_input.pop("_preloop_origin", None)
    if payload.session_id or payload.model:
        tool_input["_preloop_origin"] = {
            "session_id": (payload.session_id or "").strip() or None,
            "model": (payload.model or "").strip() or None,
        }
    origin_id: Optional[str] = None
    hosted_origin = False
    if payload.session_id and payload.session_id.strip():
        origin = await run_db_off_loop(
            lambda: _origin_runtime_session_id(
                identity, payload.source, payload.session_id.strip()
            )
        )
        if origin is not None:
            origin_id = origin.runtime_session_id
            hosted_origin = origin.hosted_runner
        if origin_id:
            tool_input["_preloop_origin"]["runtime_session_id"] = origin_id
    if payload.cwd:
        tool_input["cwd"] = payload.cwd
        cwd_session_id = origin_id or identity.runtime_session_id
        if cwd_session_id is not None:
            await run_db_off_loop(
                lambda: _record_session_cwd(identity, cwd_session_id, payload.cwd)
            )
    # The approval model intentionally has no adapter column. Preserve the
    # non-sensitive origin alongside the native tool input so approver
    # surfaces can distinguish the adapter without a schema migration.
    if payload.source and payload.source.strip():
        tool_input["_preloop_source"] = payload.source.strip()
    # Repository identity rides the same way as the source marker. Only the
    # fields the hook actually set are stored, so an absent remote stays
    # absent instead of materializing as an empty string.
    if payload.repository is not None:
        tool_input["_preloop_repository"] = payload.repository.model_dump(
            exclude_unset=True, exclude_none=True
        )

    flow_policy_label: Optional[str] = None
    if (
        payload.flow_execution_id
        and identity.managed_agent_id is not None
        and payload.session_id
        and payload.session_id.strip()
    ):
        grant = await run_db_off_loop(lambda: _flow_approval_grant(identity, payload))
        if grant is not None:
            # The flow's own governance applies to its session.
            identity = replace(identity, flow_id=grant.flow_id)
            if tool_call_within_policy(
                grant,
                tool_name=payload.tool_name,
                tool_input=payload.tool_input or {},
                cwd=payload.cwd,
            ):
                flow_policy_label = grant.flow_name
                logger.info(
                    "Flow %s approval policy covers %s for execution %s",
                    grant.flow_id,
                    payload.tool_name,
                    grant.execution_id,
                )

    decision, reason, request_id, timed_out = await request_agent_permission(
        base_url=_permission_check_base_url(),
        account_id=identity.account_id,
        user_id=identity.user_id,
        managed_agent_id=identity.managed_agent_id,
        # A runner-hosted session's approvals belong on that session, where
        # the console and `preloop sessions attach` show them (#1482).
        runtime_session_id=(
            UUID(origin_id)
            if hosted_origin and origin_id
            else identity.runtime_session_id
        ),
        managed_agent_name=identity.managed_agent_name,
        api_key_id=identity.api_key_id,
        source=payload.source,
        tool_name=payload.tool_name,
        tool_input=tool_input,
        agent_reasoning=payload.agent_reasoning,
        client_decision=payload.client_decision,
        evaluation_phase=payload.evaluation_phase,
        flow_id=identity.flow_id,
        flow_policy_label=flow_policy_label,
        approval_request_id=payload.approval_request_id,
        flow_execution_id=identity.flow_execution_id,
    )
    parked = decision == "park"
    if parked:
        decision = "deny"
    # Claim after the decision, so a Cursor ask response is never called delivered.
    # Copilot has no note carrier. Codex PermissionRequest and Cursor before*
    # hooks defer until carrying output, or become terminal when the run ends.
    defer_note_delivery = (
        payload.source == "copilot_cli"
        or (
            payload.source == "codex_cli" and payload.evaluation_phase != "pre_tool_use"
        )
        or (
            payload.source == "cursor"
            and (
                payload.hook_event_name != "preToolUse"
                or decision not in {"allow", "deny"}
            )
        )
    )
    # These native tool gates have no model-visible note field. Keep notes
    # pending for a carrying model transport instead of claiming or spooling
    # a block that the adapter cannot deliver.
    operator_note = None
    if payload.source not in {
        "opencode",
        "pi",
        "deepseek",
        "hermes",
        "openclaw",
        "nanobot",
        "devin_cli",
        "copilot_cli",
    }:
        operator_note = await run_db_off_loop(
            lambda: _claim_operator_note(
                identity,
                origin_session_id=origin_id,
                defer_delivery=defer_note_delivery,
                hosted_origin=hosted_origin,
            )
        )
    activity_session_id = origin_id or identity.runtime_session_id
    if activity_session_id is not None and _records_native_tool_call(payload):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        await run_db_off_loop(
            lambda: _record_native_tool_call(
                identity,
                runtime_session_id=activity_session_id,
                payload=payload,
                decision=decision,
                reason=reason,
                approval_request_id=request_id,
                timed_out=timed_out,
                elapsed_ms=elapsed_ms,
            )
        )
    decided_by = None
    if request_id and not timed_out and not parked:
        decided_by = await run_db_off_loop(
            lambda: _approval_decided_by(identity, request_id)
        )
    return AgentPermissionCheckResponse(
        decision=decision,
        reason=reason,
        request_id=request_id,
        timed_out=timed_out,
        operator_note=operator_note,
        decided_by=decided_by,
        parked=parked,
    )


def _approval_decided_by(
    identity: PermissionIdentity, request_id: str
) -> Optional[str]:
    """Name who decided the approval, for the terminal; never raises."""
    try:
        from preloop.models.crud import crud_user
        from preloop.models.crud.approval_request import crud_approval_request

        with get_session_factory()() as db:
            row = crud_approval_request.get(
                db, id=UUID(str(request_id)), account_id=identity.account_id
            )
            if row is None or row.status not in ("approved", "declined"):
                return None
            if row.decided_by_ai:
                return "AI review"
            if row.auto_approved_reason:
                return "policy"
            wanted = row.status
            for vote in reversed(row.responses or []):
                if vote.get("decision") != wanted:
                    continue
                try:
                    user = crud_user.get(db, id=UUID(str(vote.get("user_id"))))
                except ValueError:
                    return None
                return getattr(user, "username", None) if user else None
            return None
    except Exception:
        logger.warning("Could not resolve the approval decider", exc_info=True)
        return None


class ApprovalExpireRequest(BaseModel):
    """Why the waiter gave up on an approval."""

    reason: Literal["hook_deadline"] = "hook_deadline"
    waited_seconds: Optional[int] = Field(None, ge=0, le=86400)


class ApprovalExpireResponse(BaseModel):
    """The approval's status after the call; a decision that landed first wins."""

    approval_request_id: str
    status: str
    approver_comment: Optional[str] = None
    decided_by: Optional[str] = Field(
        None, description="Who decided, when a decision landed before the expiry"
    )


def _owned_approval_exists(identity: PermissionIdentity, approval_id: UUID) -> bool:
    """True when the approval belongs to the caller's account and agent or key."""
    from preloop.models.crud.approval_request import crud_approval_request

    with get_session_factory()() as db:
        row = crud_approval_request.get(db, id=approval_id)
        if row is None or str(row.account_id) != identity.account_id:
            return False
        if identity.managed_agent_id is not None:
            return row.managed_agent_id == identity.managed_agent_id
        return row.api_key_id == identity.api_key_id


@router.post(
    "/approvals/{approval_id}/expire",
    response_model=ApprovalExpireResponse,
    tags=["Agent Permissions"],
)
async def expire_approval_by_hook(
    approval_id: UUID,
    payload: ApprovalExpireRequest,
    authorization: Optional[str] = Header(None),
) -> ApprovalExpireResponse:
    """Expire an approval whose permission hook stopped waiting.

    The hook proposes the approval id on permission-check, so it can name the
    request even while that call is still held. Only the agent (or key) that
    raised the approval may expire it. A pending request becomes ``expired``
    and is broadcast, so no surface keeps offering Approve or Deny.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    identity = await run_db_off_loop(lambda: _resolve_permission_identity(token))
    if not await run_db_off_loop(lambda: _owned_approval_exists(identity, approval_id)):
        raise HTTPException(status_code=404, detail="Approval request not found")

    from preloop.models.db.session import get_async_db_session
    from preloop.services.approval_service import ApprovalService

    minutes = ""
    if payload.waited_seconds:
        minutes = f" within {max(1, round(payload.waited_seconds / 60))} min"
    detail = f"Expired: the agent's permission hook stopped waiting{minutes}"
    async with get_async_db_session() as db:
        service = ApprovalService(db, _permission_check_base_url())
        updated = await service.expire_request(
            approval_id, reason=payload.reason, detail=detail
        )
    if updated is None:
        raise HTTPException(status_code=404, detail="Approval request not found")
    decided_by = None
    if updated.status in ("approved", "declined"):
        decided_by = await run_db_off_loop(
            lambda: _approval_decided_by(identity, str(updated.id))
        )
    return ApprovalExpireResponse(
        approval_request_id=str(updated.id),
        status=updated.status,
        approver_comment=updated.approver_comment,
        decided_by=decided_by,
    )


def _flow_approval_grant(
    identity: PermissionIdentity, payload: AgentPermissionCheckRequest
) -> Optional[FlowApprovalGrant]:
    """Resolve the flow approval grant for this hook call; never raises."""
    try:
        with get_session_factory()() as db:
            return resolve_flow_grant(
                db,
                account_id=identity.account_id,
                managed_agent_id=identity.managed_agent_id,
                execution_id=str(payload.flow_execution_id),
                session_id=(payload.session_id or "").strip(),
                cwd=payload.cwd,
            )
    except Exception:
        logger.warning("Flow approval grant lookup failed", exc_info=True)
        return None


class AgentSessionStartRequest(BaseModel):
    """A harness session starting (or taking its first prompt) under a hook.

    Sent by the SessionStart and UserPromptSubmit hooks (#1045). Every field
    but ``session_id`` is optional, and the call is idempotent: the hook may
    repeat it, and a repeat only fills what is still empty.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(
        ..., min_length=1, max_length=200, description="The harness's own session id"
    )
    source: Optional[str] = Field(
        None, max_length=64, description="Agent adapter, e.g. 'claude_code'"
    )
    cwd: Optional[str] = Field(None, max_length=4096, description="Working directory")
    parent_session_id: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "Runtime session id of the session that spawned this one, as the "
            "hook read it from PRELOOP_PARENT_SESSION_ID. Must be in the "
            "caller's account; ignored otherwise. Write-once."
        ),
    )
    alias: Optional[str] = Field(
        None,
        max_length=4096,
        description="Spawn-time label from PRELOOP_SESSION_ALIAS; sanitized to 64 characters, write-once.",
    )
    first_prompt: Optional[str] = Field(
        None,
        max_length=20000,
        description="The first user prompt. Only a redacted 120 character title is kept.",
    )
    hook_event_name: Optional[str] = Field(
        None,
        max_length=64,
        description=(
            "The hook that sent this call. UserPromptSubmit (Cursor "
            "beforeSubmitPrompt) records the time of a prompt, never its text."
        ),
    )


class AgentSessionStartResponse(BaseModel):
    """The runtime session the hook's harness session maps to."""

    runtime_session_id: str
    parent_session_id: Optional[str] = None
    started_at: str
    created: bool
    alias: Optional[str] = None


def _register_session_start(
    identity: PermissionIdentity, payload: AgentSessionStartRequest
) -> Optional[Any]:
    from preloop.services.agent_session_lineage import register_session_start

    principal_type = identity.runtime_principal_type
    principal_id = identity.runtime_principal_id
    if not principal_type or not principal_id:
        return None
    with get_session_factory()() as db:
        result = register_session_start(
            db,
            account_id=identity.account_id,
            principal_type=principal_type,
            principal_id=principal_id,
            principal_name=identity.managed_agent_name,
            external_session_id=payload.session_id,
            agent_kind=(payload.source or "").strip() or principal_type,
            cwd=(payload.cwd or "").strip() or None,
            parent_session_id=payload.parent_session_id,
            first_prompt=payload.first_prompt,
            alias=payload.alias,
            actor_user_id=identity.user_id,
        )
        if result is not None and is_prompt_event(payload.hook_event_name):
            try:
                record_prompt(
                    db,
                    account_id=identity.account_id,
                    runtime_session_id=UUID(result.runtime_session_id),
                    api_key_id=identity.api_key_id,
                    actor_user_id=identity.user_id,
                    source=payload.source,
                    hook_event_name=(payload.hook_event_name or "").strip(),
                    managed_agent_id=identity.managed_agent_id,
                )
            except Exception:
                # The prompt timestamp is analytics; registration already
                # succeeded and the hook must not fail on it.
                db.rollback()
                logger.warning("Could not record the prompt event", exc_info=True)
        return result


@router.post(
    "/agents/session-start",
    response_model=AgentSessionStartResponse,
    tags=["Agent Permissions"],
)
async def agent_session_start(
    payload: AgentSessionStartRequest,
    authorization: Optional[str] = Header(None),
) -> AgentSessionStartResponse:
    """Register a hook-governed harness session and its parent (#1045).

    Returns the runtime session id the spawner can record, which is the id
    ``send_note`` and ``list_sessions`` take. The session is keyed exactly as
    the gateway keys the same conversation, so it is one row whichever of
    the two sees it first.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    identity = await run_db_off_loop(lambda: _resolve_permission_identity(token))
    from preloop.models.crud.runtime_session import SpawnAliasConflictError

    try:
        result = await run_db_off_loop(
            lambda: _register_session_start(identity, payload)
        )
    except SpawnAliasConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "The session id is not usable, or this credential carries no "
                "runtime principal to key the session on."
            ),
        )
    return AgentSessionStartResponse(
        runtime_session_id=result.runtime_session_id,
        parent_session_id=result.parent_session_id,
        started_at=result.started_at.isoformat(),
        created=result.created,
        alias=result.alias,
    )


class AgentSessionEventRequest(BaseModel):
    """API payload for a harness turn-end or session-end signal (#1889).

    Accepts Stop, SubagentStop and SessionEnd event names, storing the kind
    and receipt time without content. Producer integration is separate from
    this backend contract.
    """

    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(
        ..., min_length=1, max_length=200, description="The harness's own session id"
    )
    hook_event_name: Literal[
        "Stop",
        "SubagentStop",
        "SessionEnd",
        "stop",
        "sessionEnd",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
    ]
    source: Optional[str] = Field(
        None, max_length=64, description="Agent adapter, e.g. 'claude_code'"
    )
    tool_use_id: Optional[str] = Field(None, max_length=200)
    tool_name: Optional[str] = Field(None, max_length=100)
    exit_code: Optional[int] = None
    is_error: Optional[bool] = None
    duration_ms: Optional[int] = Field(None, ge=0)
    notification_type: Optional[str] = Field(None, max_length=64)
    interrupted: Optional[bool] = Field(
        None, description="Stop only: the user interrupted the turn"
    )


def _record_session_event(
    identity: PermissionIdentity, payload: AgentSessionEventRequest
) -> bool:
    """Resolve the hook's session and record the event; False when unknown."""
    from preloop.models.crud import crud_runtime_session
    from preloop.services.agent_session_headers import normalize_session_id
    from preloop.services.agent_session_lineage import (
        runner_session_for_external,
        session_source_id_for,
    )

    principal_type = identity.runtime_principal_type
    principal_id = identity.runtime_principal_id
    external = normalize_session_id(payload.session_id)
    if not principal_type or not principal_id or external is None:
        return False
    with get_session_factory()() as db:
        session = runner_session_for_external(
            db,
            account_id=identity.account_id,
            external_session_id=external,
            actor_user_id=identity.user_id,
        )
        if session is None:
            qualified = session_source_id_for(principal_id, external)
            session = crud_runtime_session.get_by_source(
                db,
                account_id=identity.account_id,
                session_source_type=principal_type,
                session_source_id=qualified,
            )
            # An account-local harness id is not authority to mutate its
            # session. Require a positively owned registration; importer
            # rows without a runtime principal cannot be ended by this hook.
            if session is not None and (
                session.runtime_principal_type != principal_type
                or session.runtime_principal_id not in {principal_id, qualified}
            ):
                return False
        if session is None:
            return False
        record_session_event(
            db,
            session=session,
            api_key_id=identity.api_key_id,
            actor_user_id=identity.user_id,
            source=payload.source,
            hook_event_name=payload.hook_event_name,
            managed_agent_id=identity.managed_agent_id,
            details=payload.model_dump(
                include={
                    "tool_use_id",
                    "tool_name",
                    "exit_code",
                    "is_error",
                    "duration_ms",
                    "notification_type",
                    "interrupted",
                },
                exclude_none=True,
            ),
        )
        return True


@router.post(
    "/agents/session-event",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    tags=["Agent Permissions"],
)
async def agent_session_event(
    payload: AgentSessionEventRequest,
    authorization: Optional[str] = Header(None),
) -> Response:
    """Record that a hook-governed turn or session ended (#1889).

    Stop and SubagentStop write a ``turn_end`` activity; SessionEnd writes
    ``session_end`` and stamps ``ended_at`` once. An unknown session, or a
    failure to record, still answers 204: a hook must never fail the harness.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Runtime bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    identity = await run_db_off_loop(lambda: _resolve_permission_identity(token))
    try:
        await run_db_off_loop(lambda: _record_session_event(identity, payload))
    except Exception:
        logger.warning("Could not record the session event", exc_info=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
