"""Explain native approval readiness using execution's routing and channel rules."""

from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import approval_readiness as crud_readiness
from preloop.models.crud import crud_approval_workflow, crud_approval_bypass
from preloop.models.crud import notification_preferences
from preloop.schemas.approval_readiness import ApprovalReadiness
from preloop.services.approval_window import resolve_approval_window
from preloop.services.event_webhooks.approval_shim import resolve_webhook_target
from preloop.services.external_approval import uses_external_link
from preloop.services.subject_governance import (
    get_account_governance_defaults,
    get_subject_governance,
    SUBJECT_TYPE_MANAGED_AGENTS,
)
from preloop.utils.email import email_is_configured
from preloop.services.push_notifications import get_apns_service, is_fcm_configured
from preloop.services.push_proxy import is_push_proxy_configured


def native_workflow(
    db: Session,
    *,
    account: models.Account,
    agent_id: UUID | None = None,
    workflow_id: UUID | None = None,
) -> models.ApprovalWorkflow | None:
    """Preview the native resolver's pins, default and first-existing fallback.

    A read must not create the fallback workflow or change enforcement.
    """
    defaults = get_account_governance_defaults(account.meta_data or {})
    agent = (
        get_subject_governance(
            account.meta_data or {},
            subject_type=SUBJECT_TYPE_MANAGED_AGENTS,
            subject_id=str(agent_id),
        )
        if agent_id
        else {}
    )
    for value in (
        workflow_id,
        agent.get("approval_workflow_id"),
        defaults.get("approval_workflow_id"),
    ):
        if not value:
            continue
        try:
            id = UUID(str(value))
        except ValueError:
            continue
        workflow = crud_approval_workflow.get(db, id=id, account_id=str(account.id))
        if workflow:
            return workflow
    default = crud_approval_workflow.get_default(db, account_id=str(account.id))
    if default:
        return default
    workflows = crud_approval_workflow.get_multi_by_account(
        db, account_id=str(account.id), limit=1
    )
    return workflows[0] if workflows else None


def describe_readiness(
    db: Session,
    *,
    account: models.Account,
    workflow: models.ApprovalWorkflow | None,
    agent_id: UUID | None = None,
    owner_id: UUID | None = None,
    pending: bool = False,
) -> ApprovalReadiness:
    """Describe configured destinations, not a guarantee that delivery succeeds."""
    result = ApprovalReadiness()
    if workflow is None:
        result.issues.append(
            "This request's approval workflow is unavailable. Review it in Approvals and configure a workflow for future requests."
            if pending
            else "No approval workflow is configured. Choose a workflow and recipients before running. The first request otherwise creates a workflow for its requester."
        )
        return result
    result.workflow_name = workflow.name
    result.workflow_id = str(workflow.id)
    # create_and_notify passes the workflow timeout as requested_seconds.
    result.timeout_seconds = resolve_approval_window(
        requested_seconds=workflow.timeout_seconds, account=account
    ).seconds
    result.external = uses_external_link(workflow)
    if result.external:
        target = resolve_webhook_target(workflow)
        if target:
            result.destinations = [target[0].capitalize()]
        else:
            result.issues.append(
                "The external approval service has no webhook destination. Configure it in the workflow before running."
            )
        return result
    if not pending and workflow.approval_mode == "ai_driven":
        result.ai_fallback = workflow.ai_fallback_behavior or "escalate"
        if result.ai_fallback != "escalate":
            return result
        if workflow.escalation_workflow_id:
            escalation = crud_approval_workflow.get(
                db, id=workflow.escalation_workflow_id, account_id=str(account.id)
            )
            if escalation:
                workflow = escalation
                result.workflow_id = str(escalation.id)
                result.workflow_name = (
                    f"{result.workflow_name} (human fallback: {workflow.name})"
                )
    users = crud_readiness.members(db, account_id=account.id)
    deciders = [user for user in users if crud_readiness.can_decide(db, user)]
    result.deciders = [user.full_name or user.username for user in deciders]
    recipient_ids = crud_readiness.recipients(db, workflow)
    recipients = [user for user in users if user.id in recipient_ids]
    result.recipients = [user.full_name or user.username for user in recipients]
    destinations: set[str] = set()
    target = resolve_webhook_target(workflow)
    if target:
        destinations.add(target[0].capitalize())
    email_configured = email_is_configured()
    for user in recipients:
        prefs = notification_preferences.get_by_user(db, user.id)
        if prefs and prefs.enable_email and user.email and email_configured:
            destinations.add("Email")
        if prefs and prefs.enable_mobile_push and prefs.notify_when_needed:
            apns_available = get_apns_service() is not None
            use_proxy = not apns_available and is_push_proxy_configured()
            ios = bool(prefs.get_device_tokens(platform="ios")) and (
                apns_available or use_proxy
            )
            android = bool(prefs.get_device_tokens(platform="android")) and (
                is_fcm_configured() or use_proxy
            )
            if ios or android:
                destinations.add("Mobile push")
    eligible_ids = {user.id for user in deciders} & recipient_ids
    destinations.update(
        crud_readiness.chat_destinations(
            db, account_id=account.id, user_ids=eligible_ids
        )
    )
    bypasses = [
        crud_approval_bypass.get_active_for_user(
            db, account_id=account.id, user_id=id, managed_agent_id=agent_id
        )
        for id in recipient_ids
    ]
    if bypasses and all(bypass is not None for bypass in bypasses):
        destinations.clear()
        result.issues.append(
            "Notifications are muted by an active approval bypass. Review the bypass before running."
        )
    result.destinations = sorted(destinations)
    if not deciders and not target:
        result.issues.append(
            "No active account member can decide approvals in the console. Add a member with permission to decide approvals."
        )
    if not recipients and not target:
        result.issues.append(
            "No active workflow recipients are configured. Choose people or a team in the approval workflow."
        )
    if not destinations:
        if owner_id and {user.id for user in recipients} == {owner_id}:
            result.issues.append(
                "Only the agent's owner receives requests, and no notification destination is available. Enable notifications or add another recipient."
            )
        else:
            result.issues.append(
                "No notification destination is available. Enable email or mobile notifications for a recipient, or configure a workflow destination."
            )
    required = workflow.approvals_required or 1
    if required > len(deciders):
        result.issues.append(
            f"This workflow requires {required} approvals, but only {len(deciders)} active members can decide in the console."
        )
    return result
