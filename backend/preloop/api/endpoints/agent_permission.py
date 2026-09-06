"""Permission-check endpoint for onboarded agents' native tool calls.

Onboarded agents call ``POST /api/v1/agents/permission-check`` (authenticated
with their managed-agent runtime bearer token) before executing a native tool.
The request is evaluated and, when human approval is required, routed through
the existing approval pipeline to mobile/watch; the endpoint blocks until a
decision (or timeout) and returns a simple allow/deny.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from preloop.api.auth.jwt import (
    _authenticate_with_api_key,
    _managed_agent_for_api_key,
    _runtime_session_id_from_api_key,
)
from preloop.api.loop_safety import run_db_off_loop
from preloop.config import settings
from preloop.models.crud import crud_api_key, crud_runtime_session
from preloop.models.db.session import get_session_factory
from preloop.services.agent_permission_service import request_agent_permission

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass(frozen=True)
class PermissionIdentity:
    """Authenticated scalars safe to retain while awaiting a human decision."""

    account_id: str
    user_id: UUID
    api_key_id: UUID
    managed_agent_id: UUID
    runtime_session_id: Optional[UUID]
    managed_agent_name: str


def _resolve_permission_identity(token: str) -> PermissionIdentity:
    """Authenticate in one worker-owned session, closed before approval waits."""
    with get_session_factory()() as db:
        api_key = crud_api_key.get_by_key(db, key=token)
        user = _authenticate_with_api_key(db, api_key)
        managed_agent = _managed_agent_for_api_key(db, api_key)
        if managed_agent is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token is not bound to a managed agent",
                headers={"WWW-Authenticate": "Bearer"},
            )
        runtime_session = None
        runtime_session_id = _runtime_session_id_from_api_key(api_key)
        if runtime_session_id is not None:
            runtime_session = crud_runtime_session.get_account_session(
                db,
                account_id=api_key.account_id,
                runtime_session_id=runtime_session_id,
            )
        return PermissionIdentity(
            account_id=str(api_key.account_id),
            user_id=user.id,
            api_key_id=api_key.id,
            managed_agent_id=managed_agent.id,
            runtime_session_id=runtime_session.id
            if runtime_session
            else runtime_session_id,
            managed_agent_name=(
                getattr(managed_agent, "display_name", None)
                or getattr(managed_agent, "name", None)
                or "Agent"
            ),
        )


def _permission_check_base_url() -> str:
    """Resolve the public Preloop base URL used in approval notifications."""
    base_url = (settings.preloop_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PRELOOP_URL is not configured",
        )
    return base_url


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
            "'opencode', 'openclaw', or 'hermes'. Stored as the "
            "'_preloop_source' marker inside tool_args so approver surfaces "
            "can label the requester."
        ),
    )
    session_id: Optional[str] = Field(None, description="Agent session id")
    cwd: Optional[str] = Field(None, description="Working directory")
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


class AgentPermissionCheckResponse(BaseModel):
    """Allow/deny decision for the agent's native tool call."""

    decision: str = Field(..., description="'allow' or 'deny'")
    reason: str = ""
    request_id: Optional[str] = None
    timed_out: bool = Field(
        False,
        description=(
            "True when the deny is only the expiry of an unanswered approval "
            "request, not a human decision. Adapters with a native 'ask' "
            "verdict may fall back to the agent's local prompt."
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
    if payload.cwd:
        tool_input["cwd"] = payload.cwd
    # The approval model intentionally has no adapter column. Preserve the
    # non-sensitive origin alongside the native tool input so approver
    # surfaces can distinguish the adapter without a schema migration.
    if payload.source and payload.source.strip():
        tool_input["_preloop_source"] = payload.source.strip()

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
    )
    return AgentPermissionCheckResponse(
        decision=decision, reason=reason, request_id=request_id, timed_out=timed_out
    )
