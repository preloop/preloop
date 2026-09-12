"""Basic model gateway budget checks shared by OSS and enterprise deployments.

Checks are preflight estimates, not atomic spend reservations. Concurrent calls
can exceed a limit before their final usage is recorded.
"""

import inspect
import uuid
from typing import Any, Dict, Optional, List, Tuple
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.model_runtime_resolver import resolve_ai_model_runtime
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.model_gateway_budget import ModelGatewayBudgetService
from preloop.models.crud import crud_managed_agent
from preloop.models.crud.budget import (
    ACCOUNT_LEVEL_SUBJECT_TYPES,
    crud_budget_policy,
    crud_budget_spend,
    get_period_start,
    spend_bucket_for_policy,
)


def _estimate_request_cost_with_optional_override(
    budget_service: ModelGatewayBudgetService,
    ai_model: models.AIModel,
    payload: Dict[str, Any],
) -> Optional[float]:
    """Estimate request cost across core versions with and without overrides."""
    pricing_override = None
    pricing_resolver = getattr(budget_service, "_pricing_override_for_request", None)
    if callable(pricing_resolver):
        pricing_override = pricing_resolver(ai_model, payload)

    estimate = budget_service._estimate_request_cost
    if "pricing_override" in inspect.signature(estimate).parameters:
        return estimate(ai_model, payload, pricing_override=pricing_override)
    return estimate(ai_model, payload)


def _resolve_managed_agent_id(
    db: Session, auth_context: ModelGatewayAuthContext
) -> Optional[uuid.UUID]:
    """Resolve the managed agent ID associated with a gateway request."""
    if not auth_context.api_key:
        return None

    context_data = (
        auth_context.api_key.context_data
        if isinstance(auth_context.api_key.context_data, dict)
        else {}
    )
    managed_agent_id = context_data.get("managed_agent_id")
    if managed_agent_id:
        try:
            return uuid.UUID(str(managed_agent_id))
        except ValueError:
            return None

    runtime_session_id = context_data.get("runtime_session_id")
    runtime_session = None
    if runtime_session_id:
        from preloop.models.crud import crud_runtime_session

        runtime_session = crud_runtime_session.get_account_session(
            db,
            account_id=str(auth_context.api_key.account_id),
            runtime_session_id=str(runtime_session_id),
        )

    from preloop.api.auth.jwt import _managed_agent_for_api_key

    managed_agent = _managed_agent_for_api_key(
        db, auth_context.api_key, runtime_session=runtime_session
    )
    return managed_agent.id if managed_agent is not None else None


def _resolve_owner_user_id(
    db: Session,
    account_id: Any,
    managed_agent_id: Optional[uuid.UUID],
) -> Optional[uuid.UUID]:
    """Resolve the owning user of a managed agent, for per-user budgets."""
    if managed_agent_id is None:
        return None
    agent = crud_managed_agent.get_for_account(
        db, account_id=str(account_id), agent_id=str(managed_agent_id)
    )
    owner_id = getattr(agent, "owner_user_id", None) if agent is not None else None
    if owner_id is None:
        return None
    try:
        return owner_id if isinstance(owner_id, uuid.UUID) else uuid.UUID(str(owner_id))
    except (TypeError, ValueError):
        return None


def _policy_lookup_subjects(
    auth_context: ModelGatewayAuthContext,
    managed_agent_id: Optional[uuid.UUID],
    owner_user_id: Optional[uuid.UUID] = None,
) -> List[Tuple[str, Optional[uuid.UUID]]]:
    """Subjects whose configured policies should be evaluated for this request."""
    subjects: List[Tuple[str, Optional[uuid.UUID]]] = [
        ("account", None),
        ("global", None),
    ]
    if auth_context.api_key:
        subjects.append(("api_key", auth_context.api_key.id))
    if managed_agent_id is not None:
        subjects.append(("managed_agent", managed_agent_id))
    # A per-user budget applies to every agent owned by that user.
    if owner_user_id is not None:
        subjects.append(("user", owner_user_id))
    return subjects


class ModelGatewayBudgetEnforcer:
    """Enforce basic BYOK budgets with optional commercial extension hooks."""

    def enforce_or_raise(
        self,
        db: Session,
        auth_context: ModelGatewayAuthContext,
        ai_model: models.AIModel,
        payload: Dict[str, Any],
    ) -> None:
        """Check budgets and raise 403 if hard limit exceeded."""
        # 1. Estimate cost
        budget_service = ModelGatewayBudgetService(db, auth_context)
        estimated_cost = _estimate_request_cost_with_optional_override(
            budget_service,
            ai_model,
            payload,
        )
        if estimated_cost is not None and estimated_cost <= 0:
            return

        if estimated_cost is not None:
            self._enforce_additional_constraints(
                db, auth_context, ai_model, payload, estimated_cost=estimated_cost
            )

        now = datetime.now(timezone.utc)
        account_id = auth_context.user.account_id

        model_alias = resolve_ai_model_runtime(
            ai_model
        ).model_gateway_model_alias or payload.get("model")

        provider = (ai_model.provider_name or "openai").lower()

        # Read candidate policies before resolving optional attribution. Accounts
        # without policies pay one indexed policy query and no agent/owner reads.
        candidates = crud_budget_policy.get_gateway_policies(
            db,
            account_id=account_id,
            ai_model_id=ai_model.id,
            model_alias=model_alias,
            api_key_id=auth_context.api_key.id if auth_context.api_key else None,
        )
        if not candidates:
            return
        subject_types = {policy.subject_type for policy in candidates}
        managed_agent_id = (
            _resolve_managed_agent_id(db, auth_context)
            if subject_types.intersection({"managed_agent", "user"})
            else None
        )
        owner_user_id = (
            _resolve_owner_user_id(db, account_id, managed_agent_id)
            if "user" in subject_types
            else None
        )
        policies_by_id = {
            policy.id: policy
            for policy in candidates
            if (
                policy.subject_type != "managed_agent"
                or (
                    managed_agent_id is not None
                    and policy.subject_id == managed_agent_id
                )
            )
            and (
                policy.subject_type != "user"
                or (owner_user_id is not None and policy.subject_id == owner_user_id)
            )
        }

        evaluations: List[
            Tuple[
                models.BudgetPolicy,
                datetime,
                Tuple[str, Optional[uuid.UUID], Optional[str]],
            ]
        ] = []
        buckets_to_fetch: List[
            Tuple[
                str,
                Optional[uuid.UUID],
                Optional[str],
                models.BudgetPeriod,
                Optional[datetime],
            ]
        ] = []
        seen_buckets: set[
            Tuple[
                str,
                Optional[uuid.UUID],
                Optional[str],
                models.BudgetPeriod,
                Optional[datetime],
            ]
        ] = set()

        for policy in policies_by_id.values():
            if (
                policy.model_alias
                and policy.model_alias != model_alias
                and not (
                    policy.subject_type == "ai_model"
                    and policy.subject_id == ai_model.id
                )
            ):
                continue

            if estimated_cost is None and policy.hard_limit_usd is not None:
                raise ModelGatewayAPIError(
                    provider=provider,
                    status_code=403,
                    message=(
                        "Cannot enforce the configured budget because this model "
                        "has no known price. Configure model pricing before retrying."
                    ),
                    code="budget_pricing_unavailable",
                )

            p_start = get_period_start(now, policy.period)
            spend_type, spend_id, spend_model_alias = spend_bucket_for_policy(policy)
            if policy.subject_type == "ai_model" and policy.subject_id is not None:
                # Legacy ID-only policies consume this model's rollup, not the
                # account-wide rollup selected by a missing stored alias.
                spend_model_alias = model_alias
            spend_model_alias = spend_model_alias or None
            bucket_key = (
                spend_type,
                spend_id,
                spend_model_alias,
                policy.period,
                p_start,
            )
            evaluations.append((policy, p_start, bucket_key))
            if bucket_key not in seen_buckets:
                seen_buckets.add(bucket_key)
                buckets_to_fetch.append(bucket_key)

        # Unpriced requests without an applicable hard limit remain usable;
        # soft-only limits cannot evaluate projected dollar spend.
        if estimated_cost is None:
            return

        spend_map: Dict[
            Tuple[
                str,
                Optional[uuid.UUID],
                Optional[str],
                models.BudgetPeriod,
                Optional[datetime],
            ],
            float,
        ] = {}
        if buckets_to_fetch:
            spend_map = crud_budget_spend.get_spend_multi(
                db=db, account_id=account_id, buckets=buckets_to_fetch
            )

        for policy, _p_start, bucket_key in evaluations:
            current_spend = spend_map.get(bucket_key, 0.0)
            projected_spend = current_spend + estimated_cost
            display_subject_type = (
                "account"
                if policy.subject_type in ACCOUNT_LEVEL_SUBJECT_TYPES
                else policy.subject_type
            )
            display_subject_id = (
                str(policy.subject_id) if policy.subject_id is not None else None
            )

            # 3. Check Soft Limit
            if policy.soft_limit_usd and projected_spend > policy.soft_limit_usd:
                if policy.notify_on_soft and current_spend <= policy.soft_limit_usd:
                    self._notify_budget_limit(
                        policy_id=policy.id,
                        account_id=policy.account_id,
                        subject_type=display_subject_type,
                        subject_id=display_subject_id,
                        limit_type="soft",
                        limit_usd=policy.soft_limit_usd,
                        current_spend_usd=projected_spend,
                    )

            # 4. Check Hard Limit
            if (
                policy.hard_limit_usd is not None
                and projected_spend > policy.hard_limit_usd
            ):
                if policy.notify_on_hard and current_spend <= policy.hard_limit_usd:
                    self._notify_budget_limit(
                        policy_id=policy.id,
                        account_id=policy.account_id,
                        subject_type=display_subject_type,
                        subject_id=display_subject_id,
                        limit_type="hard",
                        limit_usd=policy.hard_limit_usd,
                        current_spend_usd=projected_spend,
                    )

                raise ModelGatewayAPIError(
                    provider=provider,
                    status_code=403,
                    message=(
                        "Model gateway budget exceeded: "
                        f"{display_subject_type} {policy.period.name} hard limit "
                        f"of ${policy.hard_limit_usd:.2f} reached "
                        f"(current spend ${current_spend:.2f}, "
                        f"projected ${projected_spend:.2f})"
                    ),
                    code="budget_limit_exceeded",
                )

    def _enforce_additional_constraints(
        self,
        db: Session,
        auth_context: ModelGatewayAuthContext,
        ai_model: models.AIModel,
        payload: Dict[str, Any],
        *,
        estimated_cost: float,
    ) -> None:
        """Extension point for hosted credit and subscription enforcement."""

    def _notify_budget_limit(self, **kwargs: Any) -> None:
        """Extension point for commercial notification delivery."""
