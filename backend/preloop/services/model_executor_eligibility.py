"""Credential/executor compatibility, derived from execution authorization.

This is a preflight of identity compatibility, not a provider health probe or
an assertion about runner availability, budgets or model capabilities.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_managed_agent_ai_model_binding
from preloop.schemas.ai_model import ExecutorEligibility
from preloop.services.subscription_flow_use import (
    subscription_owner_user_id,
)


def hosted_model_eligibility(
    db: Session, model: models.AIModel, flow: Any
) -> ExecutorEligibility:
    """Apply the same owner opt-in predicate as trigger and gateway admission.

    Args:
        db: Database session.
        model: Selected registration.
        flow: Flow identity (account and creator).

    Returns:
        A public decision without owner identifiers or credential metadata.
    """
    from preloop.services.subscription_flow_use import flow_may_use_subscription

    if getattr(
        model, "is_principal_bound_oauth", False
    ) is not True or flow_may_use_subscription(db, flow, model):
        return ExecutorEligibility(eligible=True)
    owner = subscription_owner_user_id(db, model)
    if owner is not None and owner == str(getattr(flow, "created_by_user_id", None)):
        if str(getattr(model, "owner_flow_use_user_id", None)) != owner:
            return ExecutorEligibility(
                eligible=False,
                reason="subscription_flow_opt_in_required",
                message="The subscription owner must enable Use from my flows on this model.",
            )
    return ExecutorEligibility(
        eligible=False,
        reason="subscription_flow_owner_required",
        message="Use a flow owned by the subscription owner with Use from my flows enabled, or select an API-key or built-in model.",
    )


def model_executor_eligibility(
    db: Session, model: models.AIModel, caller: models.User
) -> dict[str, ExecutorEligibility]:
    """Describe executor identity compatibility for this caller.

    Args:
        db: Database session.
        model: Registration already authorized for reading.
        caller: Calling user. Hosted entries describe flows this user owns;
            host and interactive entries require their bound agent login.

    Returns:
        One decision per execution kind. Local entries are conditional on using
        the caller's own authorized agent; a private pool alone grants nothing.
    """
    from preloop.services.model_gateway_auth import (
        ModelGatewayAuthContext,
        _principal_authorized_model_ids,
    )

    hosted = hosted_model_eligibility(
        db,
        model,
        SimpleNamespace(account_id=caller.account_id, created_by_user_id=caller.id),
    )
    local = ExecutorEligibility(eligible=True)
    if getattr(model, "is_principal_bound_oauth", False) is True:
        agents = crud_managed_agent_ai_model_binding.agent_ids_for_user_model(
            db, account_id=caller.account_id, user_id=caller.id, model_id=model.id
        )
        allowed = any(
            str(model.id)
            in _principal_authorized_model_ids(
                db,
                ModelGatewayAuthContext(
                    token="",
                    user=caller,
                    api_key=models.ApiKey(
                        account_id=caller.account_id,
                        user_id=caller.id,
                        context_data={"managed_agent_id": str(agent_id)},
                    ),
                ),
                [model],
            )
            for agent_id in agents
        )
        local = ExecutorEligibility(
            eligible=allowed,
            reason=None if allowed else "bound_agent_login_required",
            message=(
                "Use your agent's bound login; private runners need a host profile."
                if allowed
                else "Use an agent login bound to this model, or select an API-key or built-in model."
            ),
        )
    return {
        "hosted": hosted,
        "private_runner_container": hosted.model_copy(),
        "private_runner_host": local,
        "interactive_agent": local.model_copy(),
    }


def flow_executor_eligibility(db: Session, flow: Any) -> ExecutorEligibility:
    """Read the trigger's selection check without creating an execution.

    Args:
        db: Database session.
        flow: Saved flow, whose creator determines subscription eligibility.

    Returns:
        The trigger's principal compatibility decision for the default model.
        Host profiles and persistent agents retain runtime identity admission.
    """
    from preloop.models.crud import crud_ai_model
    from preloop.services.model_routing import (
        FlowModelPrincipalError,
        validate_execution_model_principal,
    )

    try:
        validate_execution_model_principal(db, flow, {})
    except FlowModelPrincipalError:
        model = crud_ai_model.get(db, id=str(flow.ai_model_id))
        return hosted_model_eligibility(db, model, flow)
    return ExecutorEligibility(eligible=True)
