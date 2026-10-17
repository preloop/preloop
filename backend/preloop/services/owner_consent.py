"""Owner-only approval gates for runner work and new agent sessions."""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.api.loop_safety import run_db_off_loop
from preloop.models.crud import owner_consent as crud
from preloop.models.db.session import get_async_db_session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OwnerConsentResult:
    """Current state of one exact consent intent."""

    status: str
    approval_id: UUID
    created: bool


def check_owner_consent(
    db: Session,
    *,
    account_id: UUID | str,
    owner_user_id: UUID | str,
    action: str,
    subject_id: str,
    request_key: str,
    details: dict[str, Any],
    execution_id: str | None = None,
    managed_agent_id: UUID | None = None,
    requester_user_id: UUID | str | None = None,
) -> OwnerConsentResult:
    """Create or poll a durable owner request, without ever auto-approving it."""
    intent = dict(details)
    if requester_user_id is not None:
        intent["requester_user_id"] = str(requester_user_id)
    consent, approval, created = crud.get_or_create(
        db,
        account_id=UUID(str(account_id)),
        owner_user_id=UUID(str(owner_user_id)),
        action=action,
        subject_id=subject_id,
        request_key=request_key,
        details=intent,
        execution_id=execution_id,
        managed_agent_id=managed_agent_id,
    )
    status = "consumed" if consent.consumed_at is not None else approval.status
    return OwnerConsentResult(status, approval.id, created)


async def notify_owner_consent(approval_id: UUID) -> None:
    """Send through the owner's existing preferences, including mobile push."""
    from preloop.services.approval_service import (
        ApprovalService,
        _log_approval_lifecycle_async,
    )

    async with get_async_db_session() as db:
        service = ApprovalService(db, os.getenv("PRELOOP_URL", "http://localhost:8000"))
        approval = await service.get_approval_request(approval_id)
        if approval is None or approval.status != "pending":
            return
        consent = await crud.get_by_approval_async(db, approval_id)
        if consent is None or consent.notification_sent_at is not None:
            return
        workflow = crud.notification_workflow(consent, approval)
        results = await service.send_notifications(approval, workflow)
        if any(
            isinstance(channel, dict)
            and (
                channel.get("success") is True
                or isinstance(channel.get("queued"), int)
                and channel["queued"] > 0
                or isinstance(channel.get("sent"), int)
                and channel["sent"] > 0
                or channel.get("scheduled") is True
            )
            for channel in results.values()
        ):
            await crud.mark_notified(db, consent)
        _log_approval_lifecycle_async(
            str(approval.account_id),
            approval.id,
            "created",
            tool_name="owner_consent",
            execution_id=approval.execution_id,
        )


async def require_owner_consent(
    db: Session,
    *,
    account_id: UUID | str,
    owner_user_id: UUID | str,
    requester_user_id: UUID | str,
    subject_id: str,
    action: str,
    request_key: str,
    details: dict[str, Any],
    managed_agent_id: UUID | None = None,
) -> UUID:
    """Require and consume an owner decision before dispatching a new session."""
    try:
        result = await run_db_off_loop(
            lambda: check_owner_consent(
                db,
                account_id=account_id,
                owner_user_id=owner_user_id,
                requester_user_id=requester_user_id,
                subject_id=subject_id,
                action=action,
                request_key=request_key,
                details=details,
                managed_agent_id=(
                    managed_agent_id
                    or (UUID(subject_id) if action == "agent_session_start" else None)
                ),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result.created:
        try:
            await asyncio.wait_for(notify_owner_consent(result.approval_id), timeout=25)
        except Exception:
            logger.exception(
                "Owner notification pending retry for %s", result.approval_id
            )
    if result.status == "approved" and await run_db_off_loop(
        lambda: crud.consume(db, approval_id=result.approval_id)
    ):
        return result.approval_id
    raise HTTPException(
        status_code=409 if result.status == "pending" else 403,
        detail={
            "status": result.status,
            "approval_id": str(result.approval_id),
            "message": (
                "Waiting for runner owner"
                if action.startswith("runner_")
                else "Waiting for the agent owner to approve this session."
            )
            if result.status == "pending"
            else "The agent owner declined this session, or the request timed out.",
        },
    )


async def retry_owner_notifications() -> None:
    """Retry durable pending notifications, including after server restarts."""
    while True:
        try:
            async with get_async_db_session() as db:
                pending = await crud.pending_notifications(db)
            for approval_id in pending:
                try:
                    await asyncio.wait_for(
                        notify_owner_consent(approval_id), timeout=25
                    )
                except Exception:
                    logger.exception(
                        "Owner notification retry failed for %s", approval_id
                    )
        except Exception:
            logger.exception("Owner notification retry sweep failed")
        await asyncio.sleep(15)
