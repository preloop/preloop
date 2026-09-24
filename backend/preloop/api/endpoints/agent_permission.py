"""Permission-check endpoint for onboarded agents' native tool calls.

Onboarded agents call ``POST /api/v1/agents/permission-check`` (authenticated
with their managed-agent runtime bearer token) before executing a native tool.
The request is evaluated and, when human approval is required, routed through
the existing approval pipeline to mobile/watch; the endpoint blocks until a
decision (or timeout) and returns a simple allow/deny.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from preloop.api.auth.jwt import (
    _authenticate_with_api_key,
    _managed_agent_for_api_key,
    _runtime_session_id_from_api_key,
)
from preloop.api.loop_safety import run_db_off_loop
from preloop.config import settings
from preloop.models.crud import crud_api_key, crud_runtime_session
from preloop.models.db.session import get_session_factory
from preloop.services import operator_notes
from preloop.services.agent_permission_service import request_agent_permission

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class PermissionIdentity:
    """Authenticated scalars safe to retain while awaiting a human decision."""

    account_id: str
    user_id: UUID
    api_key_id: UUID
    managed_agent_id: Optional[UUID]
    runtime_session_id: Optional[UUID]
    managed_agent_name: str


def _resolve_permission_identity(token: str) -> PermissionIdentity:
    """Authenticate in one worker-owned session, closed before approval waits."""
    with get_session_factory()() as db:
        api_key = crud_api_key.get_by_key(db, key=token)
        user = _authenticate_with_api_key(db, api_key)
        managed_agent = _managed_agent_for_api_key(db, api_key)
        runtime_session = None
        runtime_session_id = _runtime_session_id_from_api_key(api_key)
        if runtime_session_id is not None:
            runtime_session = crud_runtime_session.get_account_session(
                db,
                account_id=api_key.account_id,
                runtime_session_id=runtime_session_id,
            )
        if managed_agent is None:
            context = (
                api_key.context_data if isinstance(api_key.context_data, dict) else {}
            )
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
            managed_agent_name=(
                getattr(managed_agent, "display_name", None)
                or getattr(managed_agent, "name", None)
                or (runtime_session.runtime_principal_name if runtime_session else None)
                or "Agent"
            ),
        )


def _claim_operator_note(identity: PermissionIdentity) -> Optional[str]:
    """Claim this session's pending operator notes for the hook channel.

    Never raises: a note is a bonus on this route, and a store problem must
    not turn a permission check into a denied tool call.
    """
    if identity.managed_agent_id is None:
        return None
    try:
        with get_session_factory()() as db:
            notes = operator_notes.claim_pending_notes(
                db,
                account_id=identity.account_id,
                managed_agent_id=str(identity.managed_agent_id),
                runtime_session_id=(
                    str(identity.runtime_session_id)
                    if identity.runtime_session_id
                    else None
                ),
                channel=operator_notes.CHANNEL_HOOK,
            )
            return operator_notes.render_notes_block(notes) if notes else None
    except Exception:
        logger.warning("Operator note claim failed on permission check", exc_info=True)
        return None


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
        False, description="True when the work tree has no 'origin' remote."
    )


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
    session_id: Optional[str] = Field(None, description="Agent session id")
    cwd: Optional[str] = Field(None, description="Working directory")
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
    timed_out: bool = Field(
        False,
        description=(
            "True when the deny is only the expiry of an unanswered approval "
            "request, not a human decision. It remains a denial; adapters must "
            "not replace required central approval with a local prompt."
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

    tool_input = dict(payload.tool_input or {})
    # Caller-supplied tool arguments must never carry the trust markers: only
    # the validated ``source``/``repository`` fields may set them, so a native
    # tool argument cannot spoof the adapter or repository chip.
    tool_input.pop("_preloop_repository", None)
    tool_input.pop("_preloop_source", None)
    if payload.cwd:
        tool_input["cwd"] = payload.cwd
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

    # Claimed before the approval wait, in its own short-lived session, so no
    # connection is held while a human decides. Delivery is recorded here even
    # if the tool call is then denied: the agent read the note either way.
    operator_note = await run_db_off_loop(lambda: _claim_operator_note(identity))

    decision, reason, request_id, timed_out = await request_agent_permission(
        base_url=_permission_check_base_url(),
        account_id=identity.account_id,
        user_id=identity.user_id,
        managed_agent_id=identity.managed_agent_id,
        runtime_session_id=identity.runtime_session_id,
        managed_agent_name=identity.managed_agent_name,
        api_key_id=identity.api_key_id,
        source=payload.source,
        tool_name=payload.tool_name,
        tool_input=tool_input,
        agent_reasoning=payload.agent_reasoning,
        client_decision=payload.client_decision,
        evaluation_phase=payload.evaluation_phase,
    )
    return AgentPermissionCheckResponse(
        decision=decision,
        reason=reason,
        request_id=request_id,
        timed_out=timed_out,
        operator_note=operator_note,
    )
