"""Account-owned approve-once MCP tool decisions."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models import models
from preloop.models.crud import tool_registry, crud_account_halt
from preloop.models.crud.approval_request import get_approval_request_async
from preloop.models.db.session import get_db_session, SyncApprovalSession
from preloop.models.schemas.approval_request import ApprovalRequestUpdate
from preloop.services.approval_service import ApprovalService
import os
from preloop.utils.permissions import require_permission

router = APIRouter()


class RegistryEntryResponse(BaseModel):
    """Safe route identity, observation metadata and execution approval state."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tool_source: str
    mcp_server_id: UUID | None
    server_label: str | None
    server_name: str | None = None
    first_seen_agent_name: str | None = None
    tool_name: str
    state: Literal["pending", "approved", "denied"]
    first_seen_at: datetime
    first_seen_agent_id: UUID | None
    last_seen_at: datetime
    seen_count: int
    decided_at: datetime | None
    decided_by_user_id: UUID | None
    approval_request_id: UUID | None


class RegistryDecision(BaseModel):
    """An explicit execution authorization or revocation for this route."""

    state: Literal["approved", "denied"]


@router.get("/tools/registry", response_model=list[RegistryEntryResponse])
@require_permission("view_policies")
def list_registry(
    state: Literal["pending", "approved", "denied"] | None = None,
    page: int = Query(0, ge=0),
    page_size: int = Query(50, ge=1, le=100),
    account: models.Account = Depends(get_account_for_user),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> list[RegistryEntryResponse]:
    """List only the current account's observed execution routes."""
    entries = tool_registry.list_entries(
        db, account_id=account.id, state=state, page=page, page_size=page_size
    )
    servers, agents = tool_registry.display_names(
        db, account_id=account.id, entries=entries
    )
    return [
        RegistryEntryResponse.model_validate(entry).model_copy(
            update={
                "server_name": servers.get(entry.mcp_server_id),
                "first_seen_agent_name": agents.get(entry.first_seen_agent_id),
            }
        )
        for entry in entries
    ]


@router.post(
    "/tools/registry/{entry_id}/decision", response_model=RegistryEntryResponse
)
@require_permission("manage_policies")
def decide_registry(
    entry_id: UUID,
    decision: RegistryDecision,
    account: models.Account = Depends(get_account_for_user),
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> RegistryEntryResponse:
    """Decide or revoke a route, settling its pending approval with that outcome."""
    import asyncio

    async def apply_decision() -> RegistryEntryResponse:
        """Keep blocking CRUD and approval resolution on the worker thread."""
        # The approval service locks a request before its linked registry row. Read
        # its id first and use that same order here to avoid inverse lock ordering.
        crud_account_halt.lock_account(db, account_id=account.id)
        entry = tool_registry.get(db, account_id=account.id, entry_id=entry_id)
        if entry is None:
            raise HTTPException(404, "Tool registry entry not found")
        adapter = SyncApprovalSession(db)
        request = (
            await get_approval_request_async(
                adapter, request_id=entry.approval_request_id
            )
            if entry.approval_request_id
            else None
        )
        if request is not None and request.status == "pending":
            await ApprovalService(
                adapter, os.getenv("PRELOOP_URL", "http://localhost:8000")
            ).update_approval_request(
                request.id,
                ApprovalRequestUpdate(
                    status="approved" if decision.state == "approved" else "declined",
                    resolved_at=datetime.utcnow(),
                ),
                audit_approver_id=current_user.id,
                audit_reason="Manual tool registry decision",
            )
        entry = tool_registry.decide(
            db,
            account_id=account.id,
            entry_id=entry_id,
            state=decision.state,
            user_id=current_user.id,
        )
        if entry is None:
            raise HTTPException(404, "Tool registry entry not found")
        db.commit()
        return RegistryEntryResponse.model_validate(entry)

    return asyncio.run(apply_decision())
