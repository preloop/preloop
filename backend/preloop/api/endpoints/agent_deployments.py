"""Operator initiated real agent deployment over SSH or onto a new GCE VM."""

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.auth.jwt import create_access_token
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_audit_log,
    crud_managed_agent,
    crud_managed_agent_ai_model_binding,
    crud_user_role,
)
from preloop.models.db.session import get_db_session, release_transaction
from preloop.schemas.agent_deployment import AgentDeploymentRequest
from preloop.services.agent_deployment import (
    DeploymentError,
    DeploymentResult,
    deployment_capabilities,
    install_over_ssh,
)
from preloop.services.agent_deployment_gcp import provision_gcp
from preloop.services.model_runtime_resolver import effective_gateway_alias
from preloop.utils.permissions import require_permission

router = APIRouter(prefix="/agent-deployments", tags=["Agent deployment"])


def authorize_deployment(db: Session, user: models.User) -> None:
    """Keep remote execution owner/admin only, including installations without RBAC."""
    account = crud_account.get(db, id=user.account_id)
    if not account or not account.is_active:
        raise HTTPException(403, "An active account is required")
    if user.is_superuser or account.primary_user_id == user.id:
        return
    roles = crud_user_role.get_user_roles(db, user_id=user.id)
    if any(
        role.is_system_role
        and role.name in {"owner", "admin"}
        and role.account_id in {None, user.account_id}
        for role in roles
    ):
        return
    raise HTTPException(403, "An account owner or administrator must deploy agents")


@router.get("/capabilities")
async def capabilities(
    current_user: models.User = Depends(get_current_active_user),
) -> dict[str, bool]:
    """Tell the console which backends the operator actually enabled."""
    return deployment_capabilities()


def verify_registered_agent(
    db: Session,
    *,
    account_id: str,
    model_id: str,
    runtime: str,
    alias: str,
    evidence: DeploymentResult,
) -> dict[str, Any]:
    """Do not accept a host's success message without matching local records."""
    agent = crud_managed_agent.get_for_account(
        db, account_id=account_id, agent_id=evidence.agent_id
    )
    if (
        agent is None
        or agent.agent_kind != runtime
        or agent.lifecycle_state != "active"
    ):
        raise DeploymentError(
            "The runtime was not registered as an active agent in this account"
        )
    bindings = crud_managed_agent_ai_model_binding.list_for_agent(
        db, account_id=account_id, agent_id=evidence.agent_id
    )
    if evidence.model_alias != alias or not any(
        str(binding.ai_model_id) == model_id
        and binding.gateway_alias == alias
        and binding.status == "gateway_ready"
        for binding in bindings
    ):
        raise DeploymentError("The agent did not onboard with the selected model")
    if not agent.last_seen_at or not evidence.runtime_version.strip():
        raise DeploymentError(
            "The agent has not reported presence and a runtime version"
        )
    summary = crud_managed_agent.get_summary_for_account(
        db, account_id=account_id, agent_id=evidence.agent_id
    )
    if summary is None:
        raise DeploymentError("The registered agent summary is unavailable")
    return summary


@router.post("")
@require_permission("manage_agents")
async def deploy_agent(
    payload: AgentDeploymentRequest,
    current_user: models.User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> dict[str, Any]:
    """Complete bounded installation and live onboarding before returning success.

    SSH credentials remain in this request's memory. A generated, short-lived
    user token is passed over the verified SSH connection for the CLI to mint
    the runtime's own credentials; no provider key or GCP credential is sent.
    """
    authorize_deployment(db, current_user)
    if not deployment_capabilities()[payload.target]:
        raise HTTPException(
            503, "This deployment method is not configured by the operator"
        )
    model = crud_ai_model.get(
        db, id=payload.model_id, account_id=str(current_user.account_id)
    )
    if model is None:
        raise HTTPException(404, "The selected model was not found in this account")
    alias = effective_gateway_alias(model)
    if not alias:
        raise HTTPException(400, "The selected model must have gateway routing enabled")
    account_id, user_id = str(current_user.account_id), current_user.id
    origin = settings.preloop_url.rstrip("/")
    if not origin.startswith("https://"):
        raise HTTPException(503, "Remote deployment requires an HTTPS PRELOOP_URL")
    token = create_access_token(
        {"sub": str(user_id)}, expires_delta=timedelta(minutes=20)
    )
    details = {
        "runtime": payload.runtime,
        "model_id": str(payload.model_id),
        "target": payload.target,
        "request_id": str(payload.idempotency_key),
    }
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action="agent_deployment_started",
        resource_type="agent_deployment",
        resource_id=str(payload.idempotency_key),
        status="success",
        details=details,
    )
    release_transaction(db)
    vm_name = None
    try:
        # Reserve two hundred seconds for GCE failure cleanup. The proxy/client
        # deadline is longer, so timeout never silently abandons a billable VM.
        async with asyncio.timeout(700):
            if payload.target == "gcp":
                async with provision_gcp(
                    account_id, payload.idempotency_key, payload.compute_size
                ) as (ssh, vm_name):
                    evidence = await install_over_ssh(
                        ssh,
                        runtime=payload.runtime,
                        alias=alias,
                        url=origin,
                        token=token,
                        request_id=payload.idempotency_key,
                    )
                    summary = verify_registered_agent(
                        db,
                        account_id=account_id,
                        model_id=str(payload.model_id),
                        runtime=payload.runtime,
                        alias=alias,
                        evidence=evidence,
                    )
            else:
                assert payload.ssh is not None
                evidence = await install_over_ssh(
                    payload.ssh,
                    runtime=payload.runtime,
                    alias=alias,
                    url=origin,
                    token=token,
                    request_id=payload.idempotency_key,
                )
                summary = verify_registered_agent(
                    db,
                    account_id=account_id,
                    model_id=str(payload.model_id),
                    runtime=payload.runtime,
                    alias=alias,
                    evidence=evidence,
                )
    except (DeploymentError, TimeoutError) as exc:
        message = (
            str(exc)
            if isinstance(exc, DeploymentError)
            else "Agent deployment timed out; the SSH connection was closed and any newly provisioned VM was cleaned up"
        )
        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=user_id,
            action="agent_deployment_failed",
            resource_type="agent_deployment",
            resource_id=str(payload.idempotency_key),
            status="failure",
            details={**details, "error": message},
        )
        raise HTTPException(502, message) from exc
    crud_audit_log.log_action(
        db,
        account_id=account_id,
        user_id=user_id,
        action="agent_deployment_completed",
        resource_type="managed_agent",
        resource_id=evidence.agent_id,
        status="success",
        details={
            **details,
            "runtime_version": evidence.runtime_version,
            "vm_name": vm_name,
        },
    )
    return {
        "id": str(payload.idempotency_key),
        "status": "succeeded",
        "agent_id": evidence.agent_id,
        "agent": summary,
        "runtime_version": evidence.runtime_version,
        "model_alias": alias,
        "vm_name": vm_name,
        "logs": [
            "SSH host identity verified",
            "Runtime installed and validated",
            "Agent registration and selected model verified",
        ],
    }
