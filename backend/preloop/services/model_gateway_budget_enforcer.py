"""Basic model gateway budget checks shared by OSS and enterprise deployments.

Checks are preflight estimates, not atomic spend reservations. Concurrent calls
can exceed a limit before their final usage is recorded.

A budget that cannot be evaluated is not a budget that was exceeded. When the
price catalog has no entry for the requested model, the estimate is ``None``
and no dollar comparison is possible. That used to be a 403: an account with a
$100 monthly hard limit and $0 of spend was refused outright because a new
model (``google/gemini-3.8-flash``) had not reached the catalog yet. The
missing price is Preloop's gap, not the customer's overspend, so the request is
allowed, its spend counts as zero, the usage row keeps
``pricing_available=false``, the caller gets an ``X-Preloop-Warning``, and an
admin is paged on the existing unpriced-model path so the catalog hole gets
closed instead of being silently paid for by refused traffic.

The accepted cost of that ruling: while a model is unpriced, a hard limit does
not brake it, and the per-model alert has a 24h cooldown, so an account willing
to rotate uncatalogued aliases can run up spend no dollar comparison can see.
Every such request still lands a usage row with real token counts and
``pricing_available=false``, so it is countable after the fact and repriced
once the catalog learns the model. A volume backstop for unpriced traffic is
deliberately not part of this change.
"""

import logging
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
from preloop.plugins.account_hooks import get_budget_extension
from preloop.models.crud.budget import (
    ACCOUNT_LEVEL_SUBJECT_TYPES,
    crud_budget_policy,
    crud_budget_spend,
    get_period_start,
    spend_bucket_for_policy,
)

logger = logging.getLogger(__name__)

#: Warning code echoed to gateway clients when a hard-limit budget could not be
#: enforced because the model has no known price. Kept as the old refusal code
#: so dashboards and log filters built against it keep matching; only the
#: outcome changed from "blocked" to "allowed and flagged".
UNPRICED_BUDGET_WARNING_CODE = "budget_pricing_unavailable"


def unpriced_budget_warning(model_alias: Optional[str]) -> str:
    """Warning text for an unpriced request under a hard-limit policy.

    Args:
        model_alias: Alias the request resolved to, for the operator who has
            to go and price it.

    Returns:
        A single-line, header-safe warning naming the code, the model, and
        what the customer's spend numbers will and will not show.
    """
    return (
        f"{UNPRICED_BUDGET_WARNING_CODE}: no known price for "
        f"'{model_alias or 'unknown'}', so its budget hard limit could not be "
        "enforced. The request was allowed and counted as $0.00 spend. "
        "Configure model pricing or a per-account price override to restore "
        "enforcement."
    )


def _estimate_request_cost_with_optional_override(
    budget_service: ModelGatewayBudgetService,
    ai_model: models.AIModel,
    payload: Dict[str, Any],
) -> Optional[float]:
    """Estimate cost using the shared core pricing-override contract."""
    return budget_service._estimate_request_cost(
        ai_model,
        payload,
        pricing_override=budget_service._pricing_override_for_request(
            ai_model, payload
        ),
    )


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
    ) -> Optional[str]:
        """Check budgets and raise 403 if a priced hard limit is exceeded.

        Args:
            db: Database session.
            auth_context: Authenticated gateway principal.
            ai_model: The model the request resolved to.
            payload: The gateway request body.

        Returns:
            A warning to surface to the caller when the request was allowed
            but a configured hard limit could not be enforced (no known
            price), or ``None`` when every applicable budget was evaluated.

        Raises:
            ModelGatewayAPIError: 403 when a priced request would cross a
                configured hard limit.
        """
        # 1. Estimate cost
        budget_service = ModelGatewayBudgetService(db, auth_context)
        estimated_cost = _estimate_request_cost_with_optional_override(
            budget_service,
            ai_model,
            payload,
        )
        if estimated_cost is not None and estimated_cost <= 0:
            return None

        if estimated_cost is not None:
            self._enforce_additional_constraints(
                db, auth_context, ai_model, payload, estimated_cost=estimated_cost
            )

        now = datetime.now(timezone.utc)
        account_id = auth_context.account_id

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
        # Policies of other accounts that also cover this request, such as an
        # ancestor's (account hook H5). Their spend lives under their own
        # account, so each is read from ``policy.account_id`` below.
        extension = get_budget_extension()
        if extension is not None:
            extra = list(
                extension.extra_policies(
                    db,
                    account_id=account_id,
                    auth_context=auth_context,
                    ai_model=ai_model,
                    model_alias=model_alias,
                )
                or []
            )
            if extra:
                candidates = list(candidates) + extra
        if not candidates:
            return None
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
        # Buckets of other accounts' policies (hook H5), keyed by that account.
        foreign_buckets: Dict[str, List[Any]] = {}
        own_account = str(account_id)

        unenforceable_hard_limit = False
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
                # Warn, never block: an uncatalogued price is Preloop's gap.
                unenforceable_hard_limit = True

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
            policy_account = str(policy.account_id or account_id)
            if policy_account != own_account:
                evaluations.append((policy, p_start, (policy_account, bucket_key)))
                if bucket_key not in foreign_buckets.setdefault(policy_account, []):
                    foreign_buckets[policy_account].append(bucket_key)
                continue
            evaluations.append((policy, p_start, bucket_key))
            if bucket_key not in seen_buckets:
                seen_buckets.add(bucket_key)
                buckets_to_fetch.append(bucket_key)

        # Unpriced requests remain usable whatever the policy says: no dollar
        # comparison is possible, so nothing can be shown to be exceeded. A
        # hard limit that went unenforced is reported to the caller and paged
        # to admins; a soft-only limit is silent, as it always was.
        if estimated_cost is None:
            if not unenforceable_hard_limit:
                return None
            self._alert_unpriced_model(
                db,
                account_id=account_id,
                ai_model=ai_model,
                model_alias=model_alias,
                provider=provider,
            )
            return unpriced_budget_warning(model_alias)

        spend_map: Dict[Any, float] = {}
        if buckets_to_fetch:
            spend_map = crud_budget_spend.get_spend_multi(
                db=db, account_id=account_id, buckets=buckets_to_fetch
            )
        for foreign_account, foreign in foreign_buckets.items():
            foreign_spend = crud_budget_spend.get_spend_multi(
                db=db, account_id=foreign_account, buckets=foreign
            )
            for foreign_key, amount in foreign_spend.items():
                spend_map[(foreign_account, foreign_key)] = amount

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

        return None

    @staticmethod
    def _alert_unpriced_model(
        db: Session,
        *,
        account_id: Any,
        ai_model: models.AIModel,
        model_alias: Optional[str],
        provider: str,
    ) -> None:
        """Page admins that a budget went unenforced for want of a price.

        Reuses the recording path's alert so the (model, provider) cooldown,
        the cross-replica dedup marker and the "is this a customer-owned
        endpoint we will never catalog" filter are shared rather than
        reimplemented. Never raises: a missing alert must not cost the
        customer their request.

        Args:
            db: Database session.
            account_id: Account whose budget could not be evaluated.
            ai_model: The model the request resolved to.
            model_alias: Alias recorded for the request.
            provider: Normalized provider name.
        """
        if not model_alias:
            return
        try:
            from preloop.services.unpriced_model_alert import (
                notify_unpriced_model,
                should_page_unpriced_model,
            )

            if not should_page_unpriced_model(ai_model):
                return
            notify_unpriced_model(
                db,
                account_id=str(account_id),
                model_alias=model_alias,
                provider_name=provider,
                total_tokens=0,
                ai_model=ai_model,
            )
        except Exception:  # noqa: BLE001 - alerting never fails a request
            logger.exception(
                "Unpriced-budget alert failed for provider %s alias %s",
                provider,
                model_alias,
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
