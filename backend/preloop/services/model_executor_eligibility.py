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
from preloop.models.schemas.executor_eligibility import ExecutorEligibility
from preloop.services.subscription_flow_use import (
    subscription_flow_denial_reason,
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
    if getattr(model, "is_principal_bound_oauth", False) is not True:
        return ExecutorEligibility(eligible=True)
    return _hosted_decision(subscription_flow_denial_reason(db, flow, model))


def _hosted_decision(reason: str | None) -> ExecutorEligibility:
    """Map the admission predicate's denial to public remediation."""
    messages = {
        "subscription_flow_opt_in_required": "The subscription owner must enable Use from my flows on this model.",
        "subscription_flow_owner_required": "Use a flow owned by the subscription owner with Use from my flows enabled, or select an API-key or built-in model.",
        "subscription_flow_owner_inactive": "The subscription owner must be an active member of the flow's account. Restore their membership, or select an API-key or built-in model.",
    }
    return ExecutorEligibility(
        eligible=reason is None,
        reason=reason,
        message=messages[reason] if reason is not None else None,
    )


def model_executor_eligibilities(
    db: Session, registrations: list[models.AIModel], caller: models.User
) -> dict[str, dict[str, ExecutorEligibility]]:
    """Describe a catalog's credential compatibility with two batch reads.

    Args:
        db: Database session.
        registrations: Registrations already authorized for reading.
        caller: User whose flows and active agent logins are being described.

    Returns:
        Model ids mapped to executor decisions. Shared resource handles are
        excluded by the caller. No policy or provider health is promised.
    """
    from preloop.services.model_gateway_auth import configured_agent_model_ids

    subscriptions = [
        model
        for model in registrations
        if getattr(model, "is_principal_bound_oauth", False) is True
    ]
    owner_sets = (
        crud_managed_agent_ai_model_binding.subscription_owners_for_models(
            db, account_id=caller.account_id, ai_models=subscriptions
        )
        if subscriptions
        else {}
    )
    owners = {
        key: (next(iter(value)) or None) if len(value) == 1 else None
        for key, value in owner_sets.items()
    }
    bindings = (
        crud_managed_agent_ai_model_binding.list_for_user_models(
            db,
            account_id=caller.account_id,
            user_id=caller.id,
            model_ids=[str(model.id) for model in subscriptions],
        )
        if subscriptions
        else []
    )
    allowed = configured_agent_model_ids(bindings, subscriptions)
    flow = SimpleNamespace(account_id=caller.account_id, created_by_user_id=caller.id)
    result: dict[str, dict[str, ExecutorEligibility]] = {}
    for model in registrations:
        hosted = local = ExecutorEligibility(eligible=True)
        if getattr(model, "is_principal_bound_oauth", False) is True:
            hosted = _hosted_decision(
                subscription_flow_denial_reason(
                    db, flow, model, owner_user=caller, subscription_owners=owners
                )
            )
            eligible = str(model.id) in allowed
            local = ExecutorEligibility(
                eligible=eligible,
                reason=None if eligible else "bound_agent_login_required",
                message=(
                    "Use your agent's bound login; private runners need a host profile."
                    if eligible
                    else "Use an agent login bound to this model, or select an API-key or built-in model."
                ),
            )
        result[str(model.id)] = {
            "hosted": hosted,
            "private_runner_container": hosted.model_copy(),
            "private_runner_host": local,
            "interactive_agent": local.model_copy(),
        }
    return result


def model_executor_eligibility(
    db: Session, model: models.AIModel, caller: models.User
) -> dict[str, ExecutorEligibility]:
    """Describe executor identity compatibility for one readable registration.

    Args:
        db: Database session.
        model: Registration already authorized for reading.
        caller: User whose flows and bound agent logins are described.

    Returns:
        One decision per execution kind, with no credential identifiers.
    """
    return model_executor_eligibilities(db, [model], caller)[str(model.id)]


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
