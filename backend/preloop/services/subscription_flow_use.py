"""Owner opt-in for hosted flows that use a subscription-OAuth model.

A subscription-OAuth model (ChatGPT/Codex or Claude subscription credentials,
``AIModel.is_principal_bound_oauth``) normally serves only the managed agent
whose onboarding bound it. Its owner can additionally allow *their own* flows
to use it from hosted executions. The rule has three parts and all must hold:

- **Subscription owner.** Every active agent bound to the model's credential
  lineage (rows sharing its credential secret) has the same
  ``ManagedAgent.owner_user_id``. No owner, or two owners, means nobody.
- **Opt-in.** ``AIModel.owner_flow_use_user_id`` equals that owner. Only the
  owner can write it (account admins cannot set it for someone else).
- **Flow owner.** ``Flow.created_by_user_id`` equals that owner, in the same
  account. A flow with no recorded owner is never eligible.

The gateway stays the only holder of the refresh token. Hosted runs and the
owner's local agent refresh through the same per-secret row lock in
``SecretService``, so they cannot fork the token lineage.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_flow_execution,
    crud_managed_agent_ai_model_binding,
    crud_user,
)

logger = logging.getLogger(__name__)

# ``ApiUsage.meta_data["subscription_use"]`` on rows a flow execution served
# through its owner's opted-in subscription.
OWNER_FLOW_SUBSCRIPTION_USE = "owner_flow"


def subscription_owner_user_id(db: Session, ai_model: models.AIModel) -> Optional[str]:
    """Return the single user who owns a subscription-OAuth model.

    Args:
        db: Active database session.
        ai_model: Model row to inspect.

    Returns:
        The owner's user id, or ``None`` when the model is not
        subscription-bound, has no active bound agent, or its agents have no
        owner or different owners.
    """
    if not bool(getattr(ai_model, "is_principal_bound_oauth", False)):
        return None
    if ai_model.account_id is None:
        return None
    owners = crud_managed_agent_ai_model_binding.subscription_owner_user_ids(
        db, account_id=str(ai_model.account_id), ai_model=ai_model
    )
    if len(owners) != 1:
        return None
    owner = next(iter(owners))
    return owner or None


def owner_flow_use_enabled(db: Session, ai_model: models.AIModel) -> bool:
    """Whether the model's current subscription owner has opted in.

    Args:
        db: Active database session.
        ai_model: Model row to inspect.

    Returns:
        ``True`` only when the stored opt-in names the current owner.
    """
    opted_in = getattr(ai_model, "owner_flow_use_user_id", None)
    if opted_in is None:
        return False
    owner = subscription_owner_user_id(db, ai_model)
    return owner is not None and owner == str(opted_in)


def flow_may_use_subscription(db: Session, flow: Any, ai_model: models.AIModel) -> bool:
    """Whether a hosted execution of ``flow`` may use ``ai_model``.

    Args:
        db: Active database session.
        flow: Flow row (or a duck-typed stand-in with ``account_id`` and
            ``created_by_user_id``).
        ai_model: Subscription-OAuth model row the execution selected.

    Returns:
        ``True`` when the flow's owner owns the subscription, opted in, and is
        still an active member of the flow's account.
    """
    flow_owner = getattr(flow, "created_by_user_id", None)
    flow_account = getattr(flow, "account_id", None)
    if flow_owner is None or flow_account is None:
        return False
    if str(ai_model.account_id) != str(flow_account):
        return False
    opted_in = getattr(ai_model, "owner_flow_use_user_id", None)
    if opted_in is None or str(opted_in) != str(flow_owner):
        return False
    if subscription_owner_user_id(db, ai_model) != str(flow_owner):
        return False
    user = crud_user.get(db, id=flow_owner)
    return (
        user is not None
        and bool(user.is_active)
        and str(user.account_id) == str(flow_account)
    )


def owner_flow_authorized_model_ids(
    db: Session,
    *,
    account_id: Any,
    context_data: dict[str, Any],
    bound_models: list[models.AIModel],
) -> frozenset[str]:
    """Subscription rows a flow-execution runtime credential may use.

    Only a flow-execution credential (``runtime_principal.type ==
    "flow_execution"``) whose execution belongs to the named flow in the
    credential's account qualifies. This runs on the gateway hot path, so it
    issues no query unless some row carries an opt-in, then resolves the
    execution and flow once and checks the owner user once.

    Args:
        db: Active database session.
        account_id: Account of the authenticated credential.
        context_data: ``ApiKey.context_data`` of the credential.
        bound_models: Subscription-OAuth rows of the account inventory.

    Returns:
        Ids of the rows this execution may use (often empty).
    """
    candidates = [
        ai_model
        for ai_model in bound_models
        if getattr(ai_model, "owner_flow_use_user_id", None) is not None
    ]
    if not candidates:
        return frozenset()
    principal = context_data.get("runtime_principal") or {}
    if not isinstance(principal, dict) or principal.get("type") != "flow_execution":
        return frozenset()
    execution_id = context_data.get("flow_execution_id")
    flow_id = context_data.get("flow_id")
    if not execution_id or not flow_id or str(principal.get("id")) != str(execution_id):
        return frozenset()
    flow = crud_flow_execution.get_flow_for_execution(
        db, execution_id=str(execution_id), flow_id=str(flow_id)
    )
    if flow is None or str(flow.account_id) != str(account_id):
        return frozenset()
    owner = flow.created_by_user_id
    candidates = [
        ai_model
        for ai_model in candidates
        if owner is not None
        and str(ai_model.owner_flow_use_user_id) == str(owner)
        and str(ai_model.account_id) == str(flow.account_id)
    ]
    if not candidates:
        return frozenset()
    user = crud_user.get(db, id=owner)
    if (
        user is None
        or not user.is_active
        or str(user.account_id) != str(flow.account_id)
    ):
        return frozenset()
    return frozenset(
        str(ai_model.id)
        for ai_model in candidates
        if subscription_owner_user_id(db, ai_model) == str(owner)
    )
