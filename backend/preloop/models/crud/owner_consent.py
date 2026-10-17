"""Durable, account-scoped owner consent requests."""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.account_halt import crud_account_halt


def runner_for_execution(
    db: Session, *, account_id: UUID, execution_id: UUID
) -> str | None:
    """Keep a queued flow attached to the owner it already asked."""
    row = (
        db.query(models.OwnerConsent)
        .join(
            models.ApprovalRequest,
            models.OwnerConsent.approval_request_id == models.ApprovalRequest.id,
        )
        .filter(
            models.OwnerConsent.account_id == account_id,
            models.OwnerConsent.action == "runner_flow",
            models.ApprovalRequest.execution_id == str(execution_id),
        )
        .order_by(models.OwnerConsent.created_at, models.OwnerConsent.id)
        .first()
    )
    return row.subject_id if row is not None else None


def get_or_create(
    db: Session,
    *,
    account_id: UUID,
    owner_user_id: UUID,
    action: str,
    subject_id: str,
    request_key: str,
    details: dict[str, Any],
    execution_id: str | None = None,
    managed_agent_id: UUID | None = None,
) -> tuple[models.OwnerConsent, models.ApprovalRequest, bool]:
    """Serialize intent creation and persist its approval in one transaction."""
    crud_account_halt.lock_account(db, account_id=account_id)
    consent = (
        db.query(models.OwnerConsent)
        .filter_by(account_id=account_id, action=action, request_key=request_key)
        .first()
    )
    if consent is not None:
        approval = db.get(models.ApprovalRequest, consent.approval_request_id)
        if (
            consent.owner_user_id != owner_user_id
            or consent.subject_id != subject_id
            or approval is None
            or approval.tool_args != details
            or approval.execution_id != execution_id
            or approval.managed_agent_id != managed_agent_id
        ):
            db.rollback()
            raise ValueError("Consent intent does not match the original request")
        if approval.status == "pending" and approval.expires_at <= datetime.utcnow():
            approval.status = "expired"
            approval.resolved_at = datetime.utcnow()
            db.add(
                models.ApprovalEvent(
                    account_id=account_id,
                    approval_request_id=approval.id,
                    event_type="expired",
                    detail="Owner permission request timed out",
                )
            )
        db.commit()
        return consent, approval, False

    owner = (
        db.query(models.User)
        .filter_by(id=owner_user_id, account_id=account_id, is_active=True)
        .first()
    )
    if owner is None:
        db.rollback()
        raise ValueError("An active account owner is required for consent")
    default = (
        db.query(models.ApprovalWorkflow)
        .filter_by(account_id=account_id, is_default=True)
        .first()
    )
    from preloop.services.approval_window import resolve_approval_window

    account = db.get(models.Account, account_id)
    timeout = resolve_approval_window(workflow=default, account=account).seconds
    workflow = models.ApprovalWorkflow(
        account_id=account_id,
        name=f"Owner consent {uuid4()}",
        approval_type="standard",
        approval_mode="standard",
        approver_user_ids=[owner_user_id],
        approver_team_ids=[],
        approvals_required=1,
        timeout_seconds=timeout,
        is_default=False,
    )
    db.add(workflow)
    db.flush()
    config = (
        db.query(models.ToolConfiguration)
        .filter_by(
            account_id=account_id,
            tool_name="owner_consent",
            tool_source="builtin",
            managed_agent_id=None,
            mcp_server_id=None,
        )
        .first()
    )
    if config is None:
        config = models.ToolConfiguration(
            account_id=account_id,
            tool_name="owner_consent",
            tool_source="builtin",
            is_enabled=False,
        )
        db.add(config)
        db.flush()
    approval = models.ApprovalRequest(
        account_id=account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="owner_consent",
        tool_args=details,
        execution_id=execution_id,
        managed_agent_id=managed_agent_id,
        status="pending",
        expires_at=datetime.utcnow() + timedelta(seconds=timeout),
        summary=(
            "Allow this flow to run on your runner?"
            if action == "runner_flow"
            else "Allow this account member to start a session on your runner?"
            if action == "runner_session_start"
            else "Allow this account member to start a session on your agent?"
        ),
    )
    db.add(approval)
    db.flush()
    consent = models.OwnerConsent(
        account_id=account_id,
        owner_user_id=owner_user_id,
        action=action,
        subject_id=subject_id,
        request_key=request_key,
        approval_request_id=approval.id,
    )
    db.add(consent)
    db.add(
        models.ApprovalEvent(
            account_id=account_id,
            approval_request_id=approval.id,
            event_type="approval_requested",
            detail="Owner permission requested",
        )
    )
    db.commit()
    return consent, approval, True


def consume(db: Session, *, approval_id: UUID) -> bool:
    """Claim an approved intent once before a new session can be dispatched."""
    consent = (
        db.query(models.OwnerConsent)
        .filter_by(approval_request_id=approval_id)
        .with_for_update()
        .one()
    )
    approval = db.get(models.ApprovalRequest, approval_id)
    if consent.consumed_at is not None or approval.status != "approved":
        db.rollback()
        return False
    consent.consumed_at = datetime.utcnow()
    db.commit()
    return True


async def get_by_approval_async(
    db: AsyncSession, approval_id: UUID
) -> models.OwnerConsent | None:
    """Find the immutable owner binding for an approval."""
    return await db.run_sync(
        lambda session: session.query(models.OwnerConsent)
        .filter_by(approval_request_id=approval_id)
        .first()
    )


async def pending_notifications(db: AsyncSession) -> list[UUID]:
    """Recover unsent owner requests after process or transport failures."""
    result = await db.execute(
        select(models.OwnerConsent.approval_request_id)
        .join(
            models.ApprovalRequest,
            models.ApprovalRequest.id == models.OwnerConsent.approval_request_id,
        )
        .where(
            models.OwnerConsent.notification_sent_at.is_(None),
            models.ApprovalRequest.status == "pending",
            models.ApprovalRequest.expires_at > datetime.utcnow(),
        )
        .order_by(models.OwnerConsent.created_at)
        .limit(100)
    )
    return list(result.scalars().all())


async def mark_notified(db: AsyncSession, consent: models.OwnerConsent) -> None:
    """Persist completion only after a delivery or durable queue acceptance."""
    consent.notification_sent_at = datetime.utcnow()
    await db.commit()


def poll_runner_execution(
    db: Session, *, account_id: UUID, execution_id: UUID | str
) -> str | None:
    """Poll and expire existing owner consent even while its runner is offline."""
    row = (
        db.query(models.ApprovalRequest)
        .join(
            models.OwnerConsent,
            models.OwnerConsent.approval_request_id == models.ApprovalRequest.id,
        )
        .filter(
            models.OwnerConsent.account_id == account_id,
            models.OwnerConsent.action == "runner_flow",
            models.ApprovalRequest.execution_id == str(execution_id),
        )
        .order_by(models.OwnerConsent.created_at, models.OwnerConsent.id)
        .with_for_update(of=models.ApprovalRequest)
        .first()
    )
    if row is None:
        return None
    if row.status == "pending" and row.expires_at <= datetime.utcnow():
        row.status = "expired"
        row.resolved_at = datetime.utcnow()
        db.add(
            models.ApprovalEvent(
                account_id=account_id,
                approval_request_id=row.id,
                event_type="expired",
                detail="Owner permission request timed out",
            )
        )
    status = row.status
    db.commit()
    return status


def belongs_to_owner(
    db: Session, *, approval_id: UUID, account_id: UUID, owner_user_id: UUID
) -> bool:
    """Authorize one consent without granting access to the approval queue."""
    return (
        db.query(models.OwnerConsent.id)
        .filter_by(
            approval_request_id=approval_id,
            account_id=account_id,
            owner_user_id=owner_user_id,
        )
        .first()
        is not None
    )


def notification_workflow(
    consent: models.OwnerConsent, approval: models.ApprovalRequest
) -> models.ApprovalWorkflow:
    """Constrain notification delivery to the durable resource owner."""
    workflow = approval.approval_workflow
    workflow.approver_user_ids = [consent.owner_user_id]
    workflow.approver_team_ids = []
    workflow.approval_mode = "standard"
    workflow.approval_type = "standard"
    workflow.channel_configs = None
    workflow.escalation_user_ids = []
    workflow.escalation_team_ids = []
    workflow.approvals_required = 1
    return workflow


def status_for_requester(
    db: Session, *, approval_id: UUID, account_id: UUID, user_id: UUID
) -> dict[str, Any] | None:
    """Expose only the decision to the bound owner or session requester."""
    row = (
        db.query(models.OwnerConsent, models.ApprovalRequest)
        .join(
            models.ApprovalRequest,
            models.OwnerConsent.approval_request_id == models.ApprovalRequest.id,
        )
        .filter(
            models.OwnerConsent.approval_request_id == approval_id,
            models.OwnerConsent.account_id == account_id,
            models.ApprovalRequest.account_id == account_id,
        )
        .first()
    )
    if row is None:
        return None
    consent, approval = row
    requester = (approval.tool_args or {}).get("requester_user_id")
    if consent.owner_user_id != user_id and requester != str(user_id):
        return None
    status = approval.status
    if status == "pending" and approval.expires_at <= datetime.utcnow():
        status = "expired"
    return {"status": status, "expires_at": approval.expires_at}
